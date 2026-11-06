# Extract-AllZips.ps1
# 将当前目录下所有 *.zip 解压到当前目录（不创建新文件夹）

$zipFiles = Get-ChildItem -Path . -Filter *.zip -File

if ($zipFiles.Count -eq 0) {
    Write-Host "当前文件夹下没有找到 .zip 文件。" -ForegroundColor Yellow
    exit
}

foreach ($zip in $zipFiles) {
    Write-Host "正在解压: $($zip.Name) ..." -ForegroundColor Cyan
    try {
        Expand-Archive -Path $zip.FullName -DestinationPath . -Force
        Write-Host "  完成: $($zip.Name)" -ForegroundColor Green
    }
    catch {
        Write-Host "  失败: $($zip.Name) - $($_.Exception.Message)" -ForegroundColor Red
    }
}

Write-Host "全部处理完毕。" -ForegroundColor Green