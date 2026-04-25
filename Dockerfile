# syntax=docker/dockerfile:1
FROM pulumi/pulumi-python:3.231.0
ARG MNAME
ARG PULUMI_ENVIRONMENT
COPY requirements.txt ./

# Make the shell prompt show the Pulumi project name to give context.
RUN echo "PS1='pulumi (${MNAME})$ '" >> /root/.bashrc

# Install Python dependencies from requirements.txt
RUN export PIP_DEFAULT_TIMEOUT=100 && \
    python3 -m pip install --upgrade pip setuptools wheel && \
    python3 -m pip install -r requirements.txt

# When the launcher detects cloud providers from requirements.txt (pulumi-azure,
# pulumi-gcp, pulumi-aws), it passes AZURE_LOGIN_REQUIRED, GCP_LOGIN_REQUIRED,
# AWS_LOGIN_REQUIRED. Emit a one-line reminder after login for each.
RUN <<'CLOUD_NOTICES'
cat <<'CLOUD_NOTICES_BASHRC' >> /root/.bashrc
if [ "$AZURE_LOGIN_REQUIRED" = "1" ]; then
    printf "\033[36mINFO : Azure project detected. Run 'az login' before Pulumi commands; verify with 'az account show'.\033[0m\n"
fi
if [ "$GCP_LOGIN_REQUIRED" = "1" ]; then
    printf "\033[36mINFO : GCP project detected. Run 'gcloud auth login' before Pulumi commands; verify with 'gcloud auth list'.\033[0m\n"
fi
if [ "$AWS_LOGIN_REQUIRED" = "1" ]; then
    printf "\033[36mINFO : AWS project detected. Run 'aws configure' before Pulumi commands; verify with 'aws sts get-caller-identity'.\033[0m\n"
fi
CLOUD_NOTICES_BASHRC
CLOUD_NOTICES

# Check for cloud platforms in requirements.txt and install the necessary tools.
# Use explicit /bin/bash -lc invocations instead of heredocs to avoid shell
# parsing issues on different Docker/BuildKit versions.
RUN /bin/bash -lc 'if grep -q "pulumi-azure" requirements.txt; then \
    set -e; \
    apt-get update; \
    apt-get install -y curl ca-certificates; \
    curl -sL https://aka.ms/InstallAzureCLIDeb | bash; \
  fi'

RUN /bin/bash -lc 'if grep -q "pulumi-gcp" requirements.txt; then \
    set -e; \
    apt-get update && apt-get install -y curl; \
    curl -sSL https://sdk.cloud.google.com | bash -s -- --disable-prompts --install-dir=/usr/local; \
    /usr/local/google-cloud-sdk/bin/gcloud components install gke-gcloud-auth-plugin --quiet; \
    echo "export PATH=\"/usr/local/google-cloud-sdk/bin:\$PATH\"" >> /root/.bashrc; \
  fi'

RUN /bin/bash -lc 'if grep -q "pulumi-aws" requirements.txt; then \
    set -e; \
    apt-get update; \
    cd /tmp; \
    apt-get install -y curl unzip; \
    curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"; \
    unzip awscliv2.zip; \
    ./aws/install; \
  fi'

# Default to an interactive bash shell.
ENTRYPOINT ["/bin/bash"]
