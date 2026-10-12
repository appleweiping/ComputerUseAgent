# Start the pinned self-hosted Skyvern 1.0.47 and PostgreSQL containers for the
# historical pilot. Private state (database, artifacts, logs, local credentials)
# lives in $env:CUA_PRIVATE_ROOT\pilots\skyvern, which must be outside the repository.
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($env:CUA_PRIVATE_ROOT)) {
    throw "CUA_PRIVATE_ROOT must name a private directory outside the repository."
}
if ([string]::IsNullOrWhiteSpace($env:OPENAI_API_KEY)) {
    throw "OPENAI_API_KEY is required in the current process environment."
}
if ([string]::IsNullOrWhiteSpace($env:OPENAI_API_BASE)) {
    if ([string]::IsNullOrWhiteSpace($env:OPENAI_BASE_URL)) {
        throw "OPENAI_BASE_URL or OPENAI_API_BASE is required."
    }
    $env:OPENAI_API_BASE = $env:OPENAI_BASE_URL
}
if ([string]::IsNullOrWhiteSpace($env:OPENAI_BASE_URL)) {
    $env:OPENAI_BASE_URL = $env:OPENAI_API_BASE
}

$repository = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$private = Join-Path ([System.IO.Path]::GetFullPath($env:CUA_PRIVATE_ROOT)) "pilots\skyvern"
if ($private.StartsWith($repository + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "CUA_PRIVATE_ROOT must resolve outside the repository."
}
$directories = @(
    $private,
    (Join-Path $private "postgres"),
    (Join-Path $private "artifacts"),
    (Join-Path $private "videos"),
    (Join-Path $private "har"),
    (Join-Path $private "log"),
    (Join-Path $private "downloads"),
    (Join-Path $private "browser_sessions"),
    (Join-Path $private "credential_vault"),
    (Join-Path $private ".skyvern")
)
foreach ($directory in $directories) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}

docker network inspect week1-skyvern-repro *> $null
if ($LASTEXITCODE -ne 0) {
    docker network create week1-skyvern-repro | Out-Null
}

docker inspect week1-skyvern-postgres *> $null
if ($LASTEXITCODE -ne 0) {
    $postgresArgs = @(
        "run", "-d",
        "--name", "week1-skyvern-postgres",
        "--network", "week1-skyvern-repro",
        "-e", "POSTGRES_USER=skyvern",
        "-e", "POSTGRES_PASSWORD=skyvern",
        "-e", "POSTGRES_DB=skyvern",
        "-v", "$private\postgres:/var/lib/postgresql/data",
        "--health-cmd", "pg_isready -U skyvern",
        "--health-interval", "5s",
        "--health-timeout", "5s",
        "--health-retries", "12",
        "postgres@sha256:f1341c01408dc7278e9d365ed4f860cd3f87dd16b4464ac326fc0f422083a579"
    )
    & docker @postgresArgs | Out-Null
}
else {
    docker start week1-skyvern-postgres | Out-Null
}

$deadline = [DateTime]::UtcNow.AddMinutes(2)
do {
    $health = docker inspect --format "{{.State.Health.Status}}" week1-skyvern-postgres
    if ($health -eq "healthy") {
        break
    }
    Start-Sleep -Seconds 3
} while ([DateTime]::UtcNow -lt $deadline)
if ($health -ne "healthy") {
    throw "PostgreSQL did not become healthy."
}

docker inspect week1-skyvern *> $null
if ($LASTEXITCODE -ne 0) {
    $skyvernArgs = @(
        "run", "-d",
        "--name", "week1-skyvern",
        "--network", "week1-skyvern-repro",
        "--add-host", "host.docker.internal:host-gateway",
        "-p", "127.0.0.1:8000:8000",
        "-e", "OPENAI_API_KEY",
        "-e", "OPENAI_BASE_URL",
        "-e", "OPENAI_API_BASE",
        "-e", "ENABLE_OPENAI=true",
        "-e", "LLM_KEY=OPENAI_GPT5_5",
        "-e", "SECONDARY_LLM_KEY=OPENAI_GPT5_5",
        "-e", "ENV=local",
        "-e", "SKYVERN_TELEMETRY=false",
        "-e", "DATABASE_STRING=postgresql+psycopg://skyvern:skyvern@week1-skyvern-postgres:5432/skyvern",
        "-e", "BROWSER_STREAMING_MODE=cdp",
        "-e", "BROWSER_TYPE=chromium-headful",
        "-e", "MAX_STEPS_PER_RUN=7",
        "-e", "LOG_LEVEL=INFO",
        "-e", "ENABLE_LOG_ARTIFACTS=false",
        "-e", "CREDENTIAL_VAULT_TYPE=skyvern",
        "-e", "ENABLE_LOCAL_CREDENTIAL_VAULT=true",
        "-e", "LOCAL_CREDENTIAL_VAULT_PATH=/data/credential_vault",
        "-v", "$private\artifacts:/data/artifacts",
        "-v", "$private\videos:/data/videos",
        "-v", "$private\har:/data/har",
        "-v", "$private\log:/data/log",
        "-v", "$private\downloads:/data/downloads",
        "-v", "$private\browser_sessions:/data/browser_sessions",
        "-v", "$private\credential_vault:/data/credential_vault",
        "-v", "$private\.skyvern:/app/.skyvern",
        "public.ecr.aws/skyvern/skyvern@sha256:ff9ae1aa20f27d90115614f98fc0a796dcb86ee218aedde9f035cd9c2a89b95b"
    )
    & docker @skyvernArgs | Out-Null
}
else {
    docker start week1-skyvern | Out-Null
}

$deadline = [DateTime]::UtcNow.AddMinutes(6)
$ready = $false
do {
    try {
        $response = Invoke-WebRequest `
            -UseBasicParsing `
            -Uri "http://127.0.0.1:8000/api/v1/heartbeat" `
            -TimeoutSec 5 `
            -NoProxy
        if ($response.StatusCode -eq 200) {
            $ready = $true
            break
        }
    }
    catch {
    }
    Start-Sleep -Seconds 3
} while ([DateTime]::UtcNow -lt $deadline)
if (-not $ready) {
    throw "Skyvern did not pass its heartbeat check."
}

Write-Output "Skyvern is ready at http://127.0.0.1:8000"
