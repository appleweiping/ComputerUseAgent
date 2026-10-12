$ErrorActionPreference = "Stop"

foreach ($container in @("week1-skyvern", "week1-skyvern-postgres")) {
    docker inspect $container *> $null
    if ($LASTEXITCODE -eq 0) {
        docker rm -f $container | Out-Null
    }
}

docker network inspect week1-skyvern-repro *> $null
if ($LASTEXITCODE -eq 0) {
    docker network rm week1-skyvern-repro | Out-Null
}

Write-Output "Removed the two reproduction containers and their network."
Write-Output "Private evidence and database files remain under CUA_PRIVATE_ROOT\pilots\skyvern."
