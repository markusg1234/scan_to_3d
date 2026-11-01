# Build COLMAP with AMD GPU support for Windows. See gpu/README.md.
# Safe to rerun: finished steps are skipped.
param(
    [string]$Root = (Join-Path $HOME 'colmap-hip'),
    [string]$GpuArch = 'gfx1101'
)
$ErrorActionPreference = 'Stop'

$ColmapTag = '4.2.1'
$VcpkgCommit = '9a5dfef5d1a0438a7d24856a85bd60f309d355e9'
$SdkName = 'therock-dist-windows-gfx110X-all-7.14.0a20260612'
$SdkUrl = "https://therock-nightly-tarball.s3.amazonaws.com/$SdkName.tar.gz"

$installer = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$studio = & $installer -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $studio) { throw 'Install Visual Studio Build Tools with the C++ compiler first.' }
$vcvars = Join-Path $studio 'VC\Auxiliary\Build\vcvars64.bat'

function Invoke-Checked($file, $arguments) {
    & $file @arguments
    if ($LASTEXITCODE -ne 0) { throw "$file failed with exit code $LASTEXITCODE" }
}

function Invoke-InBuildShell($command) {
    cmd /c "`"$vcvars`" >nul 2>&1 && $command"
    if ($LASTEXITCODE -ne 0) { throw "Failed: $command" }
}

function Set-FileText($path, $old, $new) {
    $text = Get-Content $path -Raw
    if ($text.Contains($new)) { return }
    if (-not $text.Contains($old)) { throw "Expected text not found in $path" }
    Set-Content $path $text.Replace($old, $new) -NoNewline
}

New-Item -ItemType Directory -Force $Root | Out-Null
Set-Location $Root
$forward = $Root.Replace('\', '/')

Write-Host '=== Sources'
if (-not (Test-Path src)) {
    Invoke-Checked git @('clone', '--depth', '1', '--branch', $ColmapTag, 'https://github.com/colmap/colmap.git', 'src')
}
if (-not (Test-Path vcpkg)) {
    Invoke-Checked git @('clone', 'https://github.com/microsoft/vcpkg.git', 'vcpkg')
    Invoke-Checked git @('-C', 'vcpkg', 'checkout', $VcpkgCommit)
}
if (-not (Test-Path vcpkg\vcpkg.exe)) {
    Invoke-Checked vcpkg\bootstrap-vcpkg.bat @('-disableMetrics')
}

Write-Host '=== COLMAP dependencies (about 40 minutes the first time)'
Push-Location src
Invoke-Checked "$Root\vcpkg\vcpkg.exe" @(
    'install', '--triplet', 'x64-windows', "--x-buildtrees-root=$Root\vb",
    "--x-packages-root=$Root\vp", "--x-install-root=$Root\vi", '--clean-after-build'
)
Pop-Location

Write-Host '=== AMD HIP SDK'
if (-not (Test-Path sdk\rocm\bin\hipcc.exe)) {
    New-Item -ItemType Directory -Force sdk\rocm | Out-Null
    $archive = "sdk\$SdkName.tar.gz"
    if (-not (Test-Path $archive)) { Invoke-Checked curl.exe @('-sSL', '--fail', '-o', $archive, $SdkUrl) }
    Invoke-Checked (Join-Path $env:SystemRoot 'System32\tar.exe') @('-xzf', $archive, '-C', 'sdk\rocm')
}

Write-Host '=== Windows fixes in COLMAP'
# Microsoft's omp.h stops at OpenMP 2.0, but the library it links has the newer function.
Set-FileText 'src\src\thirdparty\VLFeat\generic.c' '#include <omp.h>' "#include <omp.h>`nint omp_get_thread_limit(void); /* MSVC's omp.h stops at OpenMP 2.0. */"
# The Windows headers define min and max macros.
Set-FileText 'src\src\thirdparty\PoissonRecon\CMakeLists.txt' 'PRIVATE RELEASE)' 'PRIVATE RELEASE NOMINMAX)'

Write-Host '=== Configure'
# CMake cannot mix Clang and MSVC, so everything uses AMD's Clang with Microsoft's libraries.
$rocm = "$forward/sdk/rocm"
Invoke-InBuildShell (@(
    'cmake -S src -B build2 -G Ninja',
    "-DCMAKE_C_COMPILER=$rocm/lib/llvm/bin/clang.exe",
    "-DCMAKE_CXX_COMPILER=$rocm/lib/llvm/bin/clang++.exe",
    '-DCMAKE_BUILD_TYPE=Release',
    "-DCMAKE_TOOLCHAIN_FILE=$forward/vcpkg/scripts/buildsystems/vcpkg.cmake",
    '-DVCPKG_TARGET_TRIPLET=x64-windows',
    "-DVCPKG_INSTALLED_DIR=$forward/vi",
    '-DVCPKG_MANIFEST_INSTALL=OFF',
    '-DCUDA_ENABLED=OFF -DHIP_ENABLED=ON',
    "-DCMAKE_HIP_ARCHITECTURES=$GpuArch",
    "-DCMAKE_HIP_COMPILER=$rocm/lib/llvm/bin/clang++.exe",
    "-DROCM_PATH=$rocm",
    '-DGUI_ENABLED=OFF -DOPENGL_ENABLED=OFF -DONNX_ENABLED=OFF -DCGAL_ENABLED=OFF',
    '-DTESTS_ENABLED=OFF -DIPO_ENABLED=OFF'
) -join ' ')

Write-Host '=== Windows fixes in fetched libraries'
# PoseLib assumes a Linux or Mac compiler.
Set-FileText 'build2\_deps\poselib-src\CMakeLists.txt' '-O3 -Wall -Werror -fPIC -Wno-sign-compare' '-O3 -Wall -Wno-sign-compare'
# FAISS only sets its per-file SIMD flags off Windows, but Clang on Windows needs them too.
$faiss = 'build2\_deps\faiss-src\faiss\CMakeLists.txt'
$text = Get-Content $faiss -Raw
if (-not $text.Contains('Clang on Windows')) {
    $match = [regex]::Match($text, '(?s)COMPILE_OPTIONS "-march=armv8\.2-a\+sve"\s*\)\s*endif\(\)\s*(endif\(\)\s*endif\(\)\s*if\(FAISS_ENABLE_SVS\))')
    if (-not $match.Success) { throw "Expected text not found in $faiss" }
    $flags = @'
else()
    # Clang on Windows still needs the per-file SIMD flags.
    set_source_files_properties(${FAISS_SIMD_AVX2_SRC}
      TARGET_DIRECTORY faiss
      PROPERTIES COMPILE_OPTIONS "-mavx2;-mfma;-mf16c;-mpopcnt"
    )
    set_source_files_properties(${FAISS_SIMD_AVX512_SRC}
      TARGET_DIRECTORY faiss
      PROPERTIES COMPILE_OPTIONS
        "-mavx512f;-mavx512cd;-mavx512vl;-mavx512dq;-mavx512bw;-mfma;-mf16c;-mpopcnt"
    )

'@
    Set-Content $faiss $text.Insert($match.Groups[1].Index, $flags) -NoNewline
}

Write-Host '=== Build'
Invoke-InBuildShell 'cmake --build build2 -j 22'

Write-Host "=== Done. Now run: python setup_tools.py --gpu `"$Root`""
