@echo off
setlocal EnableExtensions EnableDelayedExpansion
REM Run a local Pulumi Docker shell on Windows.
REM Run from the project root; the image name is based on the current directory.
REM Developed by Andrew Tamagni

REM Some portions of this script were developed with assistance from Cursor AI. The specific underlying
REM model can vary by session and configuration. AI assistance was used for parts of code generation and
REM documentation, and all code/documentation have been reviewed, verified, and refined by humans for
REM quality and accuracy.

set "SCRIPT_NAME=%~nx0"
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
pushd "%SCRIPT_DIR%" 2>nul || (
    echo ERROR : Failed to switch to script directory.
    exit /b 1
)

for /F %%a in ('echo prompt $E ^| cmd') do set "ESC=%%a"
REM ANSI colors (green=success, cyan=info, orange=warning, red=error).
REM Disabled when not attached to a terminal.
set "COLOR_RESET=%ESC%[0m"
set "COLOR_GREEN=%ESC%[32m"
set "COLOR_CYAN=%ESC%[36m"
set "COLOR_ORANGE=%ESC%[33m"
set "COLOR_RED=%ESC%[31m"

set "BUILD_ONLY=0"
set "DESTROY_IMAGE=0"
set "YES_TO_ALL=0"
set "AZURE_LOGIN_REQUIRED=0"
set "EXIT_CODE=0"
set "PERSISTENT_VOL="
set "TEMP_BUILD_DIR="
set "DOCKER_ENV_ARGS="
set "LIB_MOUNT_ARGS="
set "IMAGE_NAME="
set "PROJECT_DIR_NAME="
set "CONTAINER_NAME="

call :parse_args %*
if errorlevel 1 goto end

call :check_docker_installed
if errorlevel 1 goto end

call :require_docker_access
if errorlevel 1 goto end

call :require_command powershell
if errorlevel 1 goto end

call :require_file "Pulumi.yaml"
if errorlevel 1 goto end

call :require_file "Dockerfile"
if errorlevel 1 goto end

call :require_file "requirements.txt"
if errorlevel 1 goto end

call :check_virtualenv_setting
if errorlevel 1 goto end

if "%DESTROY_IMAGE%"=="0" if "%BUILD_ONLY%"=="0" (
    call :check_pulumi_token
    if errorlevel 1 goto end
)

call :get_project_dir_name
if errorlevel 1 goto end

call :get_image_name
if errorlevel 1 goto end

call :msg "INFO : Project directory is !PROJECT_DIR_NAME!" "%COLOR_CYAN%"
call :msg "INFO : Docker image name is !IMAGE_NAME!" "%COLOR_CYAN%"

if "%BUILD_ONLY%"=="1" if "%DESTROY_IMAGE%"=="1" (
    call :fail "Use either --build-only or --destroy-image, not both."
    goto end
)

if "%YES_TO_ALL%"=="1" if not "%DESTROY_IMAGE%"=="1" (
    call :fail "The --yes flag only applies to --destroy-image."
    goto end
)

if "%DESTROY_IMAGE%"=="1" (
    call :destroy_image "!IMAGE_NAME!"
    goto end
)

call :setup_cloud_env
if errorlevel 1 goto end

call :setup_persistent_volume
if errorlevel 1 goto end

call :setup_lib_mount
if errorlevel 1 goto end

call :image_exists "!IMAGE_NAME!"
if "%ERRORLEVEL%"=="0" (
    call :msg "SUCCESS : Docker image !IMAGE_NAME! already exists." "%COLOR_GREEN%"
) else (
    call :build_image "!IMAGE_NAME!" "!PROJECT_DIR_NAME!"
    if errorlevel 1 goto end
)

if "%BUILD_ONLY%"=="1" (
    call :msg "SUCCESS : Build check completed. No shell launched." "%COLOR_GREEN%"
    goto end
)

call :launch_shell "!IMAGE_NAME!"
goto end

REM Parse command-line options (--build-only, --destroy-image, --yes, -h, --help).
:parse_args
if "%~1"=="" exit /b 0
if /I "%~1"=="--build-only" (
    set "BUILD_ONLY=1"
    shift
    goto parse_args
)
if /I "%~1"=="--destroy-image" (
    set "DESTROY_IMAGE=1"
    shift
    goto parse_args
)
if /I "%~1"=="--yes" (
    set "YES_TO_ALL=1"
    shift
    goto parse_args
)
if /I "%~1"=="--help" (
    call :usage
    set "EXIT_CODE=0"
    exit /b 1
)
if /I "%~1"=="-h" (
    call :usage
    set "EXIT_CODE=0"
    exit /b 1
)
call :fail "Unknown argument: %~1. Run .\%SCRIPT_NAME% --help for usage."
exit /b 1

REM Print script usage and option summary.
:usage
echo Usage: .\win_docker_pulumi_shell.bat [--build-only] [--destroy-image] [--yes]
echo.
echo Build the local Pulumi Docker image if needed, then launch a shell in it.
echo.
echo Options:
echo   --build-only      Only build the Docker image if needed, then exit
echo   --destroy-image   Remove the local Docker image for this project, then exit
echo   --yes             Skip the confirmation prompt for --destroy-image
echo   -h, --help        Show this help message
exit /b 0

REM Exit with an error if the given command is not found in PATH.
:require_command
where "%~1" >nul 2>&1
if errorlevel 1 (
    call :fail "Required command not found: %~1"
    exit /b 1
)
exit /b 0

REM Ensure Docker is installed; exit with a warning and status 1 if not.
:check_docker_installed
where docker >nul 2>&1
if errorlevel 1 (
    call :msg_stderr "WARNING : Docker Desktop is not installed. Install Docker Desktop first, then run %SCRIPT_NAME% again." "%COLOR_ORANGE%"
    set "EXIT_CODE=1"
    exit /b 1
)
exit /b 0

REM Exit with an error if the given path is not an existing regular file.
:require_file
if not exist "%~1" (
    call :fail "Required file not found: %~1"
    exit /b 1
)
exit /b 0

REM Verify Docker daemon is running and the current user can access it; exit on failure.
:require_docker_access
docker info >nul 2>&1
if errorlevel 1 (
    call :fail "Docker daemon is not reachable. On Windows, start Docker Desktop and wait until it is running, then try again."
    exit /b 1
)
exit /b 0

REM Fail if Pulumi.yaml sets virtualenv to venv (conflicts with the Docker runtime).
:check_virtualenv_setting
findstr /R /C:"^[ ][ ]*virtualenv:[ ][ ]*venv" "Pulumi.yaml" >nul 2>&1
if not errorlevel 1 (
    call :fail "virtualenv set in Pulumi.yaml, this will cause errors in docker environment."
    exit /b 1
)
exit /b 0

REM Verify that a Pulumi access token is available via PULUMI_ENV_FILE or PULUMI_ACCESS_TOKEN.
:check_pulumi_token
if defined PULUMI_ENV_FILE (
    if not exist "%PULUMI_ENV_FILE%" (
        call :fail "Pulumi env file not found: ""%PULUMI_ENV_FILE%"""
        exit /b 1
    )

    findstr /B /C:"PULUMI_ACCESS_TOKEN" "%PULUMI_ENV_FILE%" >nul 2>&1
    if errorlevel 1 (
        call :fail "PULUMI_ACCESS_TOKEN is missing from env file ""%PULUMI_ENV_FILE%"""
        exit /b 1
    )

    call :msg "INFO : Using Pulumi access token from $PULUMI_ENV_FILE" "%COLOR_CYAN%"
    exit /b 0
)

if defined PULUMI_ACCESS_TOKEN (
    call :msg "INFO : Using Pulumi access token from $PULUMI_ACCESS_TOKEN" "%COLOR_CYAN%"
    exit /b 0
)

call :fail "Set PULUMI_ACCESS_TOKEN or PULUMI_ENV_FILE before running this script."
exit /b 1

REM Set PROJECT_DIR_NAME to the basename of the current directory (used for project/image naming).
:get_project_dir_name
for %%I in ("%CD%") do set "PROJECT_DIR_NAME=%%~nxI"
if not defined PROJECT_DIR_NAME (
    call :fail "Could not determine the current project directory name."
    exit /b 1
)
exit /b 0

REM Get the Docker image tag from the directory basename (e.g. pulumi/azure-core-infrastructure).
:get_image_name
for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "$name = Split-Path -Leaf (Get-Location); $name = $name.ToLowerInvariant() -replace '[^a-z0-9_.-]', '-'; Write-Output ('pulumi/' + $name)"`) do (
    set "IMAGE_NAME=%%I"
)
if not defined IMAGE_NAME (
    call :fail "Could not determine the Docker image name from the current directory."
    exit /b 1
)
REM Get a stable container name from the image name (e.g. pulumi-azure-core-infrastructure-shell).
for /f "tokens=2 delims=/" %%I in ("!IMAGE_NAME!") do (
    set "CONTAINER_NAME=%%I-shell"
)
if not defined CONTAINER_NAME (
    set "CONTAINER_NAME=pulumi-shell"
)
exit /b 0

REM Detect cloud providers from requirements.txt and set HAS_AZURE, HAS_GCP, HAS_AWS.
:detect_cloud_providers
set "HAS_AZURE=0"
set "HAS_GCP=0"
set "HAS_AWS=0"
findstr /C:"pulumi-aws" "requirements.txt" >nul 2>&1
if not errorlevel 1 set "HAS_AWS=1"
findstr /C:"pulumi-gcp" "requirements.txt" >nul 2>&1
if not errorlevel 1 set "HAS_GCP=1"
findstr /C:"pulumi-azure" "requirements.txt" >nul 2>&1
if not errorlevel 1 set "HAS_AZURE=1"
exit /b 0

REM Return 0 if the given Docker image exists locally, non-zero otherwise.
:image_exists
docker image inspect "%~1" >nul 2>&1
exit /b %ERRORLEVEL%

REM Prompt the user for y/N confirmation; return 0 if yes, 1 if no.
:confirm_action
set "PROMPT_TEXT=%~1"
set "USER_RESPONSE="
set /p USER_RESPONSE="%PROMPT_TEXT% [y/N]: "
if /I "%USER_RESPONSE%"=="y" exit /b 0
if /I "%USER_RESPONSE%"=="yes" exit /b 0
call :msg "INFO : Operation cancelled." "%COLOR_CYAN%"
exit /b 1

REM Remove the given Docker image (and any containers using it), with optional confirmation unless --yes.
:destroy_image
set "TARGET_IMAGE=%~1"
set "CONTAINER_COUNT=0"
set "CONTAINER_IDS_FILE=%TEMP%\docker_pulumi_containers_%RANDOM%_%RANDOM%.txt"
if exist "%CONTAINER_IDS_FILE%" del /f /q "%CONTAINER_IDS_FILE%" >nul 2>&1

call :image_exists "%TARGET_IMAGE%"
if errorlevel 1 (
    call :msg "INFO : Docker image %TARGET_IMAGE% does not exist. Nothing to destroy." "%COLOR_CYAN%"
    exit /b 0
)

for /f "usebackq delims=" %%I in (`docker ps -a -q --filter "ancestor=%TARGET_IMAGE%"`) do (
    >> "%CONTAINER_IDS_FILE%" echo %%I
    set /a CONTAINER_COUNT+=1
)

call :msg "WARNING : Destroying Docker image %TARGET_IMAGE%" "%COLOR_ORANGE%"
if not "%CONTAINER_COUNT%"=="0" (
    call :msg "WARNING : %CONTAINER_COUNT% Docker containers that use this image will also be removed" "%COLOR_ORANGE%"
)

if not "%YES_TO_ALL%"=="1" (
    call :confirm_action "Continue and permanently remove Docker image %TARGET_IMAGE%?"
    if errorlevel 1 (
        if exist "%CONTAINER_IDS_FILE%" del /f /q "%CONTAINER_IDS_FILE%" >nul 2>&1
        exit /b 0
    )
) else (
    call :msg "WARNING : Skipping destroy confirmation because --yes was provided" "%COLOR_ORANGE%"
)

if not "%CONTAINER_COUNT%"=="0" (
    call :msg "WARNING : Removing containers that are using image %TARGET_IMAGE%" "%COLOR_ORANGE%"
    for /f "usebackq delims=" %%I in ("%CONTAINER_IDS_FILE%") do (
        docker rm -f "%%I" >nul 2>&1
        if errorlevel 1 (
            if exist "%CONTAINER_IDS_FILE%" del /f /q "%CONTAINER_IDS_FILE%" >nul 2>&1
            call :fail "Failed to remove one or more containers for image %TARGET_IMAGE%"
            exit /b 1
        )
    )
)

if exist "%CONTAINER_IDS_FILE%" del /f /q "%CONTAINER_IDS_FILE%" >nul 2>&1

docker image rm -f "%TARGET_IMAGE%" >nul
if errorlevel 1 (
    call :fail "Failed to destroy Docker image %TARGET_IMAGE%"
    exit /b 1
)

call :msg "SUCCESS : Docker image %TARGET_IMAGE% has been destroyed." "%COLOR_GREEN%"
exit /b 0

REM Create a temporary Docker build context with merged requirements.txt (and Dockerfile); set TEMP_BUILD_DIR.
:prepare_build_context
set "TEMP_BUILD_DIR=%TEMP%\docker_pulumi_%RANDOM%_%RANDOM%"
mkdir "%TEMP_BUILD_DIR%" >nul 2>&1
if errorlevel 1 (
    call :fail "Failed to create temporary Docker build directory"
    exit /b 1
)

if exist "..\lib\requirements.txt" (
    call :msg "INFO : Merging requirements.txt with ..\lib\requirements.txt" "%COLOR_CYAN%"
    powershell -NoProfile -Command "$lines = @(); $lines += Get-Content -LiteralPath 'requirements.txt'; $lines += Get-Content -LiteralPath '..\lib\requirements.txt'; $lines | Sort-Object -Unique | Set-Content -LiteralPath '%TEMP_BUILD_DIR%\requirements.txt'"
    if errorlevel 1 (
        call :fail "Failed to merge requirements.txt with ..\lib\requirements.txt"
        exit /b 1
    )
 ) else (
    copy /Y "requirements.txt" "%TEMP_BUILD_DIR%\requirements.txt" >nul
    if errorlevel 1 (
        call :fail "Failed to copy requirements.txt into the temporary build directory"
        exit /b 1
    )
)

copy /Y "Dockerfile" "%TEMP_BUILD_DIR%\Dockerfile" >nul
if errorlevel 1 (
    call :fail "Failed to copy Dockerfile into the temporary build directory"
    exit /b 1
)
exit /b 0

REM Extract BASE_IMAGE from the first FROM line in the Dockerfile.
:get_base_image
set "BASE_IMAGE="
for /f "usebackq tokens=2" %%I in (`findstr /B /C:"FROM " "Dockerfile"`) do (
    if not defined BASE_IMAGE set "BASE_IMAGE=%%I"
)
if not defined BASE_IMAGE (
    call :fail "Could not determine base image from Dockerfile"
    exit /b 1
)
exit /b 0

REM Pull the base image, prepare the build context, and build/tag the project Docker image.
:build_image
set "TARGET_IMAGE=%~1"
set "DISPLAY_NAME=%~2"

call :get_base_image
if errorlevel 1 exit /b 1

set "DOCKER_BUILDKIT=1"
call :prepare_build_context
if errorlevel 1 exit /b 1

call :msg "INFO : Pulling base image %BASE_IMAGE%" "%COLOR_CYAN%"
docker pull "%BASE_IMAGE%"
if errorlevel 1 (
    call :fail "Failed to pull Docker base image %BASE_IMAGE%"
    exit /b 1
)

call :msg "WARNING : Docker image %TARGET_IMAGE% was not found locally. Building it now." "%COLOR_ORANGE%"
docker build -t "%TARGET_IMAGE%" --build-arg MNAME="%DISPLAY_NAME%" --build-arg PULUMI_ENVIRONMENT="%PULUMI_ENVIRONMENT%" "%TEMP_BUILD_DIR%"
if errorlevel 1 (
    call :fail "Docker build failed for image %TARGET_IMAGE%"
    exit /b 1
)

call :msg "SUCCESS : Docker image %TARGET_IMAGE% has been built." "%COLOR_GREEN%"
exit /b 0

REM For PULUMI_ENVIRONMENT=Development, create or use a named volume and store its name in .persistent_vol and PERSISTENT_VOL.
:setup_persistent_volume
if not defined PULUMI_ENVIRONMENT exit /b 0

set "DOCKER_ENV_ARGS=%DOCKER_ENV_ARGS% -e PULUMI_ENVIRONMENT"

if /I not "%PULUMI_ENVIRONMENT%"=="Development" exit /b 0

if exist ".persistent_vol" (
    set /p PERSISTENT_VOL=<.persistent_vol
) else (
    for %%I in ("%CD%") do set "PERSISTENT_VOL=%%~nxI_%RANDOM%_%RANDOM%"
    docker volume create "!PERSISTENT_VOL!" >nul
    if errorlevel 1 (
        call :fail "Failed to create Docker volume !PERSISTENT_VOL!"
        exit /b 1
    )
    > ".persistent_vol" echo !PERSISTENT_VOL!
    if errorlevel 1 (
        call :fail "Failed to write .persistent_vol"
        exit /b 1
    )
)

call :msg "INFO : Using persistent Docker volume !PERSISTENT_VOL!" "%COLOR_CYAN%"
exit /b 0

REM Detect cloud providers from requirements.txt, set *_LOGIN_REQUIRED, and populate DOCKER_ENV_ARGS (env file, tokens, AWS vars).
:setup_cloud_env
call :detect_cloud_providers
set "AZURE_LOGIN_REQUIRED=0"
set "GCP_LOGIN_REQUIRED=0"
set "AWS_LOGIN_REQUIRED=0"
if "!HAS_AZURE!"=="1" set "AZURE_LOGIN_REQUIRED=1"
if "!HAS_GCP!"=="1" set "GCP_LOGIN_REQUIRED=1"
if "!HAS_AWS!"=="1" set "AWS_LOGIN_REQUIRED=1"

if defined PULUMI_ENV_FILE (
    set "DOCKER_ENV_ARGS=!DOCKER_ENV_ARGS! --env-file ""%PULUMI_ENV_FILE%"""

    if "!HAS_AWS!"=="1" (
        findstr /B /C:"AWS" "%PULUMI_ENV_FILE%" >nul 2>&1
        if errorlevel 1 call :msg "INFO : No AWS variables in env file; run 'aws configure' when needed." "%COLOR_CYAN%"
    )

    if "!AZURE_LOGIN_REQUIRED!"=="1" set "DOCKER_ENV_ARGS=!DOCKER_ENV_ARGS! -e AZURE_LOGIN_REQUIRED=1"
    if "!GCP_LOGIN_REQUIRED!"=="1" set "DOCKER_ENV_ARGS=!DOCKER_ENV_ARGS! -e GCP_LOGIN_REQUIRED=1"
    if "!AWS_LOGIN_REQUIRED!"=="1" set "DOCKER_ENV_ARGS=!DOCKER_ENV_ARGS! -e AWS_LOGIN_REQUIRED=1"
    if defined HOST_UID if defined HOST_GID set "DOCKER_ENV_ARGS=!DOCKER_ENV_ARGS! -e HOST_UID=!HOST_UID! -e HOST_GID=!HOST_GID!"
    exit /b 0
)

set "DOCKER_ENV_ARGS=!DOCKER_ENV_ARGS! -e PULUMI_ACCESS_TOKEN"

if "!HAS_AWS!"=="1" (
    set "HAS_AWS_VARS=0"
    for /f "tokens=1 delims==" %%I in ('set AWS 2^>nul') do (
        set "DOCKER_ENV_ARGS=!DOCKER_ENV_ARGS! -e %%I"
        set "HAS_AWS_VARS=1"
    )
)
if "!AZURE_LOGIN_REQUIRED!"=="1" set "DOCKER_ENV_ARGS=!DOCKER_ENV_ARGS! -e AZURE_LOGIN_REQUIRED=1"
if "!GCP_LOGIN_REQUIRED!"=="1" set "DOCKER_ENV_ARGS=!DOCKER_ENV_ARGS! -e GCP_LOGIN_REQUIRED=1"
if "!AWS_LOGIN_REQUIRED!"=="1" set "DOCKER_ENV_ARGS=!DOCKER_ENV_ARGS! -e AWS_LOGIN_REQUIRED=1"
if defined HOST_UID if defined HOST_GID set "DOCKER_ENV_ARGS=!DOCKER_ENV_ARGS! -e HOST_UID=!HOST_UID! -e HOST_GID=!HOST_GID!"

if "!HAS_AZURE!"=="0" if "!HAS_GCP!"=="0" if "!HAS_AWS!"=="0" (
    call :msg "WARNING : No cloud provider (pulumi-azure, pulumi-gcp, pulumi-aws) found in requirements.txt" "%COLOR_ORANGE%"
)
exit /b 0

REM If ..\lib\requirements.txt exists, add a volume mount for ..\lib into the container (LIB_MOUNT_ARGS).
:setup_lib_mount
if exist "..\lib\requirements.txt" (
    set "LIB_MOUNT_ARGS=-v ""%CD%\..\lib:/app/lib"""
    call :msg "INFO : Mounting ..\lib into the container" "%COLOR_CYAN%"
)
exit /b 0

REM Run an interactive shell in the given image with project mount, env, optional persistent volume, and shared lib mount.
:launch_shell
set "TARGET_IMAGE=%~1"
if "!AZURE_LOGIN_REQUIRED!"=="1" (
    call :msg "INFO : Azure project detected. Run 'az login' before Pulumi commands; verify with 'az account show'." "%COLOR_CYAN%"
)
if "!GCP_LOGIN_REQUIRED!"=="1" (
    call :msg "INFO : GCP project detected. Run 'gcloud auth login' before Pulumi commands; verify with 'gcloud auth list'." "%COLOR_CYAN%"
)
if "!AWS_LOGIN_REQUIRED!"=="1" (
    call :msg "INFO : AWS project detected. Run 'aws configure' before Pulumi commands; verify with 'aws sts get-caller-identity'." "%COLOR_CYAN%"
)
call :msg "SUCCESS : Launching shell in %TARGET_IMAGE%" "%COLOR_GREEN%"

REM Remove a stale shell container left after Docker Desktop was stopped, crash, or a failed run (--name must be unique).
docker container rm -f "%CONTAINER_NAME%" >nul 2>&1

if defined PERSISTENT_VOL (
    docker run -it --rm --name "%CONTAINER_NAME%" %DOCKER_ENV_ARGS% -w /app -v "%CD%:/app" -v "%PERSISTENT_VOL%:/persistent" %LIB_MOUNT_ARGS% "%TARGET_IMAGE%"
) else (
    docker run -it --rm --name "%CONTAINER_NAME%" %DOCKER_ENV_ARGS% -w /app -v "%CD%:/app" %LIB_MOUNT_ARGS% "%TARGET_IMAGE%"
)
set "RUN_EXIT=!ERRORLEVEL!"

REM Only treat Docker launch failures (125, 126, 127) as errors; container exit (e.g. exit, Ctrl+C = 130) is normal.
if !RUN_EXIT! GEQ 125 if !RUN_EXIT! LEQ 127 (
    call :fail "Failed to launch Docker shell for image %TARGET_IMAGE%"
    exit /b 1
)
exit /b 0

REM Print a message to stdout, optionally with an ANSI color code (only when attached to a terminal).
:msg
set "MSG_TEXT=%~1"
set "MSG_COLOR=%~2"
if defined ESC (
    echo %MSG_COLOR%%MSG_TEXT%%COLOR_RESET%
) else (
    echo %MSG_TEXT%
)
exit /b 0

REM Print a message to stderr, optionally with an ANSI color code (only when attached to a terminal).
:msg_stderr
set "MSG_TEXT=%~1"
set "MSG_COLOR=%~2"
if defined ESC (
    >con echo %MSG_COLOR%%MSG_TEXT%%COLOR_RESET%
) else (
    >con echo %MSG_TEXT%
)
exit /b 0

REM Print an error message to stderr and exit with status 1.
:fail
set "EXIT_CODE=1"
call :msg_stderr "ERROR : %~1" "%COLOR_RED%"
exit /b 1

REM Remove the temporary Docker build context directory on exit (cleanup handler).
:cleanup
if defined TEMP_BUILD_DIR (
    if exist "%TEMP_BUILD_DIR%" rd /s /q "%TEMP_BUILD_DIR%" >nul 2>&1
)
exit /b 0

REM Script exit point; run cleanup and exit with EXIT_CODE.
:end
call :cleanup
popd 2>nul
endlocal
exit /b %EXIT_CODE%