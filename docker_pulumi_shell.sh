#!/bin/bash
# Run a local Pulumi Docker shell on Linux.
# Run from the project root; the image name is based on the current directory.
# Developed by Andrew Tamagni

# Some portions of this script were developed with assistance from Cursor AI. The specific underlying
# model can vary by session and configuration. AI assistance was used for parts of code generation and
# documentation, and all code/documentation have been reviewed, verified, and refined by humans for
# quality and accuracy.

set -u
set -o pipefail

# Constants: ANSI colors (green=success, cyan=info, orange=warning, red=error). Disabled when not attached to a terminal.
COLOR_RESET="\033[0m"
COLOR_GREEN="\033[32m"
COLOR_CYAN="\033[36m"
COLOR_ORANGE="\033[33m"
COLOR_RED="\033[31m"

SCRIPT_NAME="$(basename "$0")"
BUILD_ONLY=0
DESTROY_IMAGE=0
YES_TO_ALL=0
AZURE_LOGIN_REQUIRED=0
DEV_VOLUME=""
BUILD_CONTEXT_DIR=""
CONTAINER_NAME=""

# Returns true (exit 0) if the output stream (e.g. stdout or stderr) is a terminal, so colors can be enabled.
color_enabled() {
    test -t "${1:-1}"
}

# Prints a message to stdout, optionally with an ANSI color code (only when stdout is a terminal).
msg() {
    local text="$1"
    local color_code="${2:-}"
    if [ -n "$color_code" ] && color_enabled 1; then
        printf "%b%s%b\n" "$color_code" "$text" "$COLOR_RESET"
    else
        printf "%s\n" "$text"
    fi
}

# Prints a message to stderr, optionally with an ANSI color code (only when stderr is a terminal).
msg_stderr() {
    local text="$1"
    local color_code="${2:-}"
    if [ -n "$color_code" ] && color_enabled 2; then
        printf "%b%s%b\n" "$color_code" "$text" "$COLOR_RESET" >&2
    else
        printf "%s\n" "$text" >&2
    fi
}

# Prints an error message to stderr and exits with status 1.
fail() {
    msg_stderr "ERROR : $1" "$COLOR_RED"
    exit 1
}

# Prompts the user for y/N confirmation; returns 0 if yes, 1 if no or when not running in a terminal. Exits if not interactive.
confirm_action() {
    local prompt_text="$1"
    local response

    if ! test -t 0; then
        fail "Confirmation required but no interactive terminal is available."
    fi

    printf "%s [y/N]: " "$prompt_text"
    read -r response
    case "$response" in
        y|Y|yes|YES|Yes)
            return 0
            ;;
        *)
            msg "INFO : Operation cancelled." "$COLOR_CYAN"
            return 1
            ;;
    esac
}

# Removes the temporary Docker build context directory on exit.
cleanup() {
    if [ -n "$BUILD_CONTEXT_DIR" ] && [ -d "$BUILD_CONTEXT_DIR" ]; then
        rm -rf "$BUILD_CONTEXT_DIR"
    fi
}

# Prints script usage and option summary to stdout.
usage() {
    cat <<'EOF'
Usage: ./docker_pulumi_shell.sh [--build-only] [--destroy-image] [--yes]

Build the local Pulumi Docker image if needed, then launch a shell in it.

Options:
  --build-only      Only build the Docker image if needed, then exit
  --destroy-image   Remove the local Docker image for this project, then exit
  --yes             Skip the confirmation prompt for --destroy-image
  -h, --help        Show this help message
EOF
}

# Validation: Docker, required files, Pulumi token
# Ensures Docker is installed; exits with a warning and status 1 if not.
check_docker_installed() {
    if ! command -v docker >/dev/null 2>&1; then
        msg_stderr "WARNING : Docker is not installed. Install Docker first, then run ./${SCRIPT_NAME} again." "$COLOR_ORANGE"
        exit 1
    fi
}

# Exits with an error if the given command is not found in PATH.
require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        fail "Required command not found: $1"
    fi
}

# Exits with an error if the given path is not an existing regular file.
require_file() {
    if [ ! -f "$1" ]; then
        fail "Required file not found: $1"
    fi
}

# Verifies Docker daemon is running and the current user can access it; exits on failure.
require_docker_access() {
    if ! docker info >/dev/null 2>&1; then
        fail "Docker is not running or the current user does not have access to it."
    fi
}

# Fails if Pulumi.yaml sets virtualenv to venv (conflicts with Docker runtime).
check_virtualenv_setting() {
    # Pulumi-managed virtualenv in Pulumi.yaml conflicts with Docker runtime
    if grep -qE '^[[:blank:]]+virtualenv\:[[:blank:]]+venv' Pulumi.yaml; then
        fail 'virtualenv set in Pulumi.yaml, this will cause errors in docker environment.'
    fi
}

# Ensures PULUMI_ACCESS_TOKEN is available (from PULUMI_ENV_FILE or env); exits if missing.
check_pulumi_token() {
    if [ -n "${PULUMI_ENV_FILE:-}" ]; then
        if [ ! -f "$PULUMI_ENV_FILE" ]; then
            fail "Pulumi env file not found: \"$PULUMI_ENV_FILE\""
        fi

        if ! grep -q '^PULUMI_ACCESS_TOKEN' "$PULUMI_ENV_FILE"; then
            fail "PULUMI_ACCESS_TOKEN is missing from env file \"$PULUMI_ENV_FILE\""
        fi

        msg "INFO : Using Pulumi access token from \$PULUMI_ENV_FILE" "$COLOR_CYAN"
        return 0
    fi

    if [ -n "${PULUMI_ACCESS_TOKEN:-}" ]; then
        msg "INFO : Using Pulumi access token from \$PULUMI_ACCESS_TOKEN" "$COLOR_CYAN"
        return 0
    fi

    fail 'Set PULUMI_ACCESS_TOKEN or PULUMI_ENV_FILE before running this script.'
}

# Prints the basename of the current directory (used for project/image naming).
get_dir_basename() {
    basename "$PWD"
}

# Returns the Docker image tag from directory basename (e.g. pulumi/azure-core-infrastructure).
get_image_tag() {
    local dir_name
    dir_name="$(get_dir_basename)"
    dir_name="$(printf "%s" "$dir_name" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9_.-]/-/g')"
    printf "pulumi/%s" "$dir_name"
}

# Derives a stable container name from the image tag (e.g. pulumi/azure-core-infrastructure -> azure-core-infrastructure-shell).
get_container_name() {
    local img="$1"
    local name="${img#*/}"

    if [ -z "$name" ] || [ "$name" = "$img" ]; then
        name="pulumi-shell"
    else
        name="${name}-shell"
    fi

    printf "%s" "$name"
}

# Prints the base image name from the first FROM line in the Dockerfile.
dockerfile_base_image() {
    awk '/^FROM[[:space:]]+/ {print $2; exit}' Dockerfile
}

# Sets HAS_PULUMI_AZURE, HAS_PULUMI_GCP, HAS_PULUMI_AWS based on requirements.txt.
detect_cloud_providers() {
    # From requirements.txt; more than one provider may be set
    HAS_PULUMI_AZURE=0
    HAS_PULUMI_GCP=0
    HAS_PULUMI_AWS=0
    grep -q 'pulumi-azure' requirements.txt && HAS_PULUMI_AZURE=1
    grep -q 'pulumi-gcp' requirements.txt && HAS_PULUMI_GCP=1
    grep -q 'pulumi-aws' requirements.txt && HAS_PULUMI_AWS=1
}

# Returns 0 if the given Docker image exists locally, non-zero otherwise.
image_exists() {
    docker image inspect "$1" >/dev/null 2>&1
}

# Removes the given Docker image (and any containers using it), with optional confirmation unless --yes.
remove_image() {
    local img="$1"
    local container_ids
    local container_count

    if ! image_exists "$img"; then
        msg "INFO : Docker image ${img} does not exist. Nothing to destroy." "$COLOR_CYAN"
        return 0
    fi

    container_ids="$(docker ps -a -q --filter "ancestor=${img}")"
    if [ -n "$container_ids" ]; then
        container_count="$(printf "%s\n" "$container_ids" | awk 'NF {count++} END {print count+0}')"
    else
        container_count=0
    fi

    msg "WARNING : Destroying Docker image ${img}" "$COLOR_ORANGE"
    if [ "$container_count" -gt 0 ]; then
        msg "WARNING : ${container_count} container(s) using this image will also be removed" "$COLOR_ORANGE"
    fi

    if [ "$YES_TO_ALL" -ne 1 ]; then
        if ! confirm_action "Continue and permanently remove Docker image ${img}?"; then
            return 0
        fi
    else
        msg "WARNING : Skipping destroy confirmation because --yes was provided" "$COLOR_ORANGE"
    fi

    if [ -n "$container_ids" ]; then
        msg "WARNING : Removing containers that are using image ${img}" "$COLOR_ORANGE"
        if ! printf "%s\n" "$container_ids" | xargs docker rm -f >/dev/null; then
            fail "Failed to remove one or more containers for image ${img}"
        fi
    fi

    if ! docker image rm -f "$img"; then
        fail "Failed to destroy Docker image ${img}"
    fi

    msg "SUCCESS : Docker image ${img} has been destroyed." "$COLOR_GREEN"
}

# Creates a temp build context with merged requirements.txt (and Dockerfile); sets BUILD_CONTEXT_DIR.
create_build_context() {
    # Optional shared lib in ../lib; merge requirements if present
    local shared_lib_path="../lib"
    BUILD_CONTEXT_DIR="$(mktemp -d)"
    if [ -z "$BUILD_CONTEXT_DIR" ] || [ ! -d "$BUILD_CONTEXT_DIR" ]; then
        fail "Failed to create temporary Docker build directory"
    fi

    if [ -f "${shared_lib_path}/requirements.txt" ]; then
        msg "INFO : Merging requirements.txt with ${shared_lib_path}/requirements.txt" "$COLOR_CYAN"
        cat requirements.txt "${shared_lib_path}/requirements.txt" | sort -u > "${BUILD_CONTEXT_DIR}/requirements.txt"
    else
        cp requirements.txt "${BUILD_CONTEXT_DIR}/requirements.txt"
    fi

    cp Dockerfile "${BUILD_CONTEXT_DIR}/Dockerfile"
}

# Pulls base image, builds the project Docker image with create_build_context, and tags it.
do_docker_build() {
    local img="$1"
    local display_name="$2"
    local base

    base="$(dockerfile_base_image)"
    if [ -z "$base" ]; then
        fail 'Could not determine base image from Dockerfile'
    fi

    export DOCKER_BUILDKIT=1

    create_build_context

    msg "INFO : Pulling base image ${base}" "$COLOR_CYAN"
    if ! docker pull "$base"; then
        fail "Failed to pull Docker base image ${base}"
    fi

    msg "WARNING : Docker image ${img} was not found locally. Building it now." "$COLOR_ORANGE"
    if ! docker build \
        -t "$img" \
        --build-arg MNAME="${display_name}" \
        --build-arg PULUMI_ENVIRONMENT="${PULUMI_ENVIRONMENT:-}" \
        "$BUILD_CONTEXT_DIR"; then
        fail "Docker build failed for image ${img}"
    fi

    msg "SUCCESS : Docker image ${img} has been built." "$COLOR_GREEN"
}

# For PULUMI_ENVIRONMENT=Development, creates/uses a named volume and stores its name in .persistent_vol and DEV_VOLUME.
setup_dev_volume() {
    # When PULUMI_ENVIRONMENT=Development, use a named volume and persist its name in .persistent_vol
    local vol_id

    if [ -n "${PULUMI_ENVIRONMENT:-}" ]; then
        RUN_ENV_ARGS+=(-e PULUMI_ENVIRONMENT)

        if [ "$PULUMI_ENVIRONMENT" = "Development" ]; then
            if [ -f .persistent_vol ]; then
                vol_id="$(cat .persistent_vol)"
            else
                vol_id="$(basename "$PWD")_$(date +"%s")"
                if ! docker volume create "$vol_id" >/dev/null; then
                    fail "Failed to create Docker volume ${vol_id}"
                fi
                if ! printf "%s\n" "$vol_id" > .persistent_vol; then
                    fail "Failed to write .persistent_vol"
                fi
            fi
            DEV_VOLUME="$vol_id"
            msg "INFO : Using persistent Docker volume ${DEV_VOLUME}" "$COLOR_CYAN"
        fi
    fi
}

# Detects cloud providers from requirements, sets *_LOGIN_REQUIRED, and populates RUN_ENV_ARGS (env file, tokens, AWS vars).
setup_cloud_env() {
    local aws_vars

    detect_cloud_providers

    AZURE_LOGIN_REQUIRED=0
    GCP_LOGIN_REQUIRED=0
    AWS_LOGIN_REQUIRED=0
    [ "$HAS_PULUMI_AZURE" -eq 1 ] && AZURE_LOGIN_REQUIRED=1
    [ "$HAS_PULUMI_GCP" -eq 1 ] && GCP_LOGIN_REQUIRED=1
    [ "$HAS_PULUMI_AWS" -eq 1 ] && AWS_LOGIN_REQUIRED=1

    if [ "$AZURE_LOGIN_REQUIRED" -eq 1 ]; then
        msg "INFO : Azure project detected. Run 'az login' before Pulumi commands; verify with 'az account show'." "$COLOR_CYAN"
    fi
    if [ "$GCP_LOGIN_REQUIRED" -eq 1 ]; then
        msg "INFO : GCP project detected. Run 'gcloud auth login' before Pulumi commands; verify with 'gcloud auth list'." "$COLOR_CYAN"
    fi
    if [ "$AWS_LOGIN_REQUIRED" -eq 1 ]; then
        msg "INFO : AWS project detected. Run 'aws configure' before Pulumi commands; verify with 'aws sts get-caller-identity'." "$COLOR_CYAN"
    fi

    if [ -n "${PULUMI_ENV_FILE:-}" ]; then
        RUN_ENV_ARGS+=(--env-file "$PULUMI_ENV_FILE")

        if [ "$HAS_PULUMI_AWS" -eq 1 ]; then
            if ! grep -q '^AWS' "$PULUMI_ENV_FILE"; then
                msg "INFO : No AWS variables in env file; run 'aws configure' when needed." "$COLOR_CYAN"
            fi
        fi

        if [ "$AZURE_LOGIN_REQUIRED" -eq 1 ]; then
            RUN_ENV_ARGS+=(-e AZURE_LOGIN_REQUIRED=1)
        fi
        if [ "$GCP_LOGIN_REQUIRED" -eq 1 ]; then
            RUN_ENV_ARGS+=(-e GCP_LOGIN_REQUIRED=1)
        fi
        if [ "$AWS_LOGIN_REQUIRED" -eq 1 ]; then
            RUN_ENV_ARGS+=(-e AWS_LOGIN_REQUIRED=1)
        fi
        return 0
    fi

    RUN_ENV_ARGS+=(-e PULUMI_ACCESS_TOKEN)

    if [ "$HAS_PULUMI_AWS" -eq 1 ]; then
        aws_vars="$(env | awk -F= '/^AWS/ {print $1}')"
        if [ -n "$aws_vars" ]; then
            while IFS= read -r v; do
                if [ -n "$v" ]; then
                    RUN_ENV_ARGS+=(-e "$v")
                fi
            done <<< "$aws_vars"
        fi
    fi

    if [ "$AZURE_LOGIN_REQUIRED" -eq 1 ]; then
        RUN_ENV_ARGS+=(-e AZURE_LOGIN_REQUIRED=1)
    fi
    if [ "$GCP_LOGIN_REQUIRED" -eq 1 ]; then
        RUN_ENV_ARGS+=(-e GCP_LOGIN_REQUIRED=1)
    fi
    if [ "$AWS_LOGIN_REQUIRED" -eq 1 ]; then
        RUN_ENV_ARGS+=(-e AWS_LOGIN_REQUIRED=1)
    fi

    if [ "$HAS_PULUMI_AZURE" -eq 0 ] && [ "$HAS_PULUMI_GCP" -eq 0 ] && [ "$HAS_PULUMI_AWS" -eq 0 ]; then
        msg "WARNING : No cloud provider (pulumi-azure, pulumi-gcp, pulumi-aws) found in requirements.txt" "$COLOR_ORANGE"
    fi
}

# If ../lib/requirements.txt exists, adds a volume mount for ../lib into the container (LIB_MOUNT_ARGS).
add_shared_lib_mount() {
    # Mount ../lib into container if it has requirements.txt
    local shared_lib_path="../lib"

    if [ -f "${shared_lib_path}/requirements.txt" ]; then
        LIB_MOUNT_ARGS+=(-v "$(readlink -f "${shared_lib_path}"):/app/lib")
        msg "INFO : Mounting ${shared_lib_path} into the container" "$COLOR_CYAN"
    fi
}

# Runs an interactive shell in the given image.
launch_shell() {
    local img="$1"
    local run_args=()

    run_args+=(--rm)
    if [ -n "$CONTAINER_NAME" ]; then
        run_args+=(--name "$CONTAINER_NAME")
    fi
    # stack_menu.py can chown new Pulumi.<stack>.yaml on the bind mount to this user (process in container is root).
    run_args+=(-e "HOST_UID=$(id -u)")
    run_args+=(-e "HOST_GID=$(id -g)")
    run_args+=("${RUN_ENV_ARGS[@]}")
    run_args+=(-w /app)
    run_args+=(-v "${PWD}:/app")

    if [ -n "$DEV_VOLUME" ]; then
        run_args+=(-v "${DEV_VOLUME}:/persistent")
    fi

    if [ "${#LIB_MOUNT_ARGS[@]}" -gt 0 ]; then
        run_args+=("${LIB_MOUNT_ARGS[@]}")
    fi

    msg "SUCCESS : Launching shell in ${img}" "$COLOR_GREEN"
    docker run -it "${run_args[@]}" "$img"
}

# Main: parse options, validate, then build (if needed) and run shell
for arg in "$@"; do
    case "$arg" in
        --build-only)
            BUILD_ONLY=1
            ;;
        --destroy-image)
            DESTROY_IMAGE=1
            ;;
        --yes)
            YES_TO_ALL=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "Unknown argument: $arg. Run ./${SCRIPT_NAME} --help for usage."
            ;;
    esac
done

# Validate tools and files before any Docker operations.
check_docker_installed
require_command mktemp
require_file Pulumi.yaml
require_file Dockerfile
require_file requirements.txt
require_docker_access
check_virtualenv_setting
if [ "$DESTROY_IMAGE" -ne 1 ] && [ "$BUILD_ONLY" -ne 1 ]; then
    check_pulumi_token
fi

PROJECT_DIR="$(get_dir_basename)"
DOCKER_IMAGE="$(get_image_tag)"
CONTAINER_NAME="$(get_container_name "$DOCKER_IMAGE")"
RUN_ENV_ARGS=()
LIB_MOUNT_ARGS=()

trap cleanup EXIT

msg "INFO : Project directory is ${PROJECT_DIR}" "$COLOR_CYAN"
msg "INFO : Docker image name is ${DOCKER_IMAGE}" "$COLOR_CYAN"

if [ "$BUILD_ONLY" -eq 1 ] && [ "$DESTROY_IMAGE" -eq 1 ]; then
    fail "Use either --build-only or --destroy-image, not both."
fi

if [ "$YES_TO_ALL" -eq 1 ] && [ "$DESTROY_IMAGE" -ne 1 ]; then
    fail "The --yes flag only applies to --destroy-image."
fi

if [ "$DESTROY_IMAGE" -eq 1 ]; then
    remove_image "$DOCKER_IMAGE"
    exit 0
fi

setup_cloud_env
setup_dev_volume
add_shared_lib_mount

if image_exists "$DOCKER_IMAGE"; then
    msg "SUCCESS : Docker image ${DOCKER_IMAGE} already exists." "$COLOR_GREEN"
else
    do_docker_build "$DOCKER_IMAGE" "$PROJECT_DIR"
fi

if [ "$BUILD_ONLY" -eq 1 ]; then
    msg "SUCCESS : Build check completed. No shell launched." "$COLOR_GREEN"
    exit 0
fi

launch_shell "$DOCKER_IMAGE"
