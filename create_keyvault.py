#!/usr/bin/env python3

# Create Azure Key Vault from Pulumi stack config; assign IAM and prompt for secrets used by __main__.py.

# Flow (normal mode): resolve stack → subscription + provider + RG (if needed) → create or reuse vault
# → optional group-based RBAC (keyvault_iam_groups) → prompt/set required secrets.
# Run from the project root (same auth as other scripts; Azure CLI must be logged in).
# Developed by Andrew Tamagni

# Some portions of this script were developed with assistance from Cursor AI. The specific underlying
# model can vary by session and configuration. AI assistance was used for parts of code generation and
# documentation, and all code/documentation have been reviewed, verified, and refined by humans for
# quality and accuracy.

# Usage: python create_keyvault.py [--stack STACK] [--check-only] [--yes]



import os
import re
import sys
import time
import yaml
import getpass
import argparse
import subprocess

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------
# Script-wide constants; ANSI scheme matches stack_menu.py (green=success, cyan=info, orange=warning, red=error).

# Legacy default secrets (used when key_vault.keys is not configured).
# For azure-dev-vms, you will typically override these via key_vault.keys in stack config.
REQUIRED_SECRETS = [
    ("pavmadminpw", "Palo Alto VM admin password"),
    ("vpnconnectionskey", "VPN connection pre-shared key"),
]

# Role so the CLI user can read/set secrets when running Pulumi (same identity as AzureCliCredential).
KEYVAULT_ROLE_FOR_USER = "Key Vault Administrator"

# Wait for vault to appear after create: poll interval (seconds) and max wait.
VAULT_POLL_INTERVAL = 15
VAULT_POLL_MAX_SECONDS = 180

# Wait for IAM propagation after role assignment before checking/creating secrets.
SECRET_CHECK_DELAY_AFTER_ROLE = 15

# ANSI color codes (disabled when stdout is not a terminal).
COLOR_RESET = "\033[0m"
COLOR_GREEN = "\033[32m"
COLOR_CYAN = "\033[36m"
COLOR_ORANGE = "\033[33m"
COLOR_RED = "\033[31m"


def color_enabled():
    """Return whether stdout is a TTY so colored msg()/msg_stderr() output is appropriate."""
    try:
        return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    except Exception:
        return False


def msg(text, color_code=None):
    """Print a line to stdout; wrap with ANSI color when enabled and color_code is set."""
    if color_code and color_enabled():
        print(f"{color_code}{text}{COLOR_RESET}")
    else:
        print(text)


def msg_stderr(text, color_code=None):
    """Print a line to stderr (errors/warnings); same color rules as msg()."""
    if color_code and color_enabled():
        print(f"{color_code}{text}{COLOR_RESET}", file=sys.stderr)
    else:
        print(text, file=sys.stderr)


def fail(text):
    """Log a red ERROR to stderr and terminate the process (non-recoverable CLI failure)."""
    msg_stderr(f"ERROR : {text}", COLOR_RED)
    raise SystemExit(1)


# -----------------------------------------------------------------------------
# Azure CLI helpers
# -----------------------------------------------------------------------------
# Wrappers around `az` for subscription, Key Vault, IAM, and secrets.


def run_az(args, check=True, capture=True):
    """
    Run `az` with the given argument list.

    Returns (True, stdout) on success; on failure prints CLI stderr (red if terminal) and returns (False, None).
    """
    cmd = ["az"] + args
    try:
        r = subprocess.run(
            cmd,
            check=check,
            capture_output=capture,
            text=True,
        )
        return (True, r.stdout if capture else None)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        err = getattr(e, "stderr", None)
        if err:
            msg_stderr(err, COLOR_RED)
        return (False, None)


def run_az_capture(args, check=False):
    """
    Run `az` and return raw stdout/stderr strings without auto-printing.

    Use when the caller needs to parse output or show a custom error (e.g. keyvault create).
    """
    cmd = ["az"] + args
    try:
        r = subprocess.run(cmd, check=False, capture_output=True, text=True)
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        return (r.returncode == 0, out, err)
    except FileNotFoundError:
        return (False, "", "az CLI not found")


# -----------------------------------------------------------------------------
# Config and environment
# -----------------------------------------------------------------------------
# Load stack config from Pulumi.<stack>.yaml; resolve project, stack name, and required keys.


def load_stack_config():
    """
    Load stack settings needed for Key Vault work from Pulumi.<bare_stack>.yaml.

    Reads: key_vault_name, location, subscriptionId, rg_prefix, optional keyvault_iam_groups / tenantId.
    Stack resolution: PULUMI_STACK → `pulumi stack` → single local Pulumi.*.yaml file.
    For names like org/stack or org/project/stack, the file is still Pulumi.<last-segment>.yaml.
    """
    if not os.path.isfile("Pulumi.yaml"):
        fail("Could not find required file: Pulumi.yaml")
    with open("Pulumi.yaml", "r", encoding="utf-8") as f:
        project = yaml.safe_load(f)["name"]

    # --- Resolve full Pulumi stack name (used for env and messages) ---
    full_stack = os.getenv("PULUMI_STACK") or ""

    if not full_stack:
        try:
            out = subprocess.run(
                ["pulumi", "stack"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            m = re.search(r"Current stack is ([^\s:]+):", out or "")
            if m:
                full_stack = m.group(1).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            full_stack = ""

    # Last resort: exactly one Pulumi.<name>.yaml in cwd → use that basename as the stack id.
    # The repo may contain a committed example file (Pulumi.sample.yaml). Exclude it
    # so the automation doesn't accidentally pick the sample when PULUMI_STACK is not set.
    if not full_stack:
        sample_stack = "sample"
        stack_files = [
            f for f in os.listdir(".")
            if f.startswith("Pulumi.")
            and f.endswith(".yaml")
            and f != "Pulumi.yaml"
            and not f"Pulumi.{sample_stack}.yaml"
        ]
        if not stack_files:
            fail(
                "No Pulumi stack selected and no Pulumi.<stack>.yaml found. "
                "Set PULUMI_STACK or run 'pulumi stack select'."
            )
        # Local file is Pulumi.<stack>.yaml where <stack> is the bare stack name.
        bare_stack = stack_files[0].replace("Pulumi.", "").replace(".yaml", "")
        full_stack = bare_stack

    # For file naming we always use only the final segment (e.g. ORG/dev -> dev).
    bare_stack = full_stack.split("/", 1)[-1]
    stack_file = f"Pulumi.{bare_stack}.yaml"
    if not os.path.isfile(stack_file):
        fail(f"Stack file not found: {stack_file}")

    with open(stack_file, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    config = data.get("config") or {}

    # Pulumi stores keys as "project:key" or sometimes unprefixed; try both.
    def get(key, default=None):
        q = f"{project}:{key}"
        if q in config:
            return config[q]
        if key in config:
            return config[key]
        return default

    raw_key_vault = get("key_vault")
    key_vault_name = None
    if isinstance(raw_key_vault, dict):
        key_vault_name = raw_key_vault.get("name")
    location = get("location") or config.get("azure-native:location")
    subscription_id = get("subscriptionId") or config.get("azure:subscriptionId")
    rg_prefix = get("rg_prefix")
    if location and " " in location:
        location = location.replace(" ", "").lower()

    if not key_vault_name:
        fail("config: key_vault.name is required in stack config.")
    if not location:
        fail("config: location (or azure-native:location) is required.")
    if not subscription_id:
        fail("config: subscriptionId (or azure:subscriptionId) is required.")
    if not rg_prefix:
        fail("config: rg_prefix is required.")

    # Optional list of AAD group object IDs: when set, RBAC is applied to groups (not ad-hoc user assignment here).
    keyvault_iam_groups = get("keyvault_iam_groups")
    if keyvault_iam_groups is not None and not isinstance(keyvault_iam_groups, list):
        keyvault_iam_groups = [keyvault_iam_groups] if keyvault_iam_groups else None
    if keyvault_iam_groups is not None and not keyvault_iam_groups:
        keyvault_iam_groups = None

    out = {
        # 'stack' is the full Pulumi stack identifier (may include org/project prefix);
        # local stack file is always Pulumi.<bare_stack>.yaml.
        "stack": full_stack,
        "project": project,
        "location": location,
        "subscription_id": subscription_id,
        "tenant_id": get("tenantId") or config.get("azure:tenantId"),
        "rg_prefix": rg_prefix,
        "keyvault_iam_groups": keyvault_iam_groups,
    }
    # Optional structured key_vault object with keys and IAM groups.
    if isinstance(raw_key_vault, dict):
        kv_obj: dict = {"name": key_vault_name}
        raw_keys = raw_key_vault.get("keys")
        if isinstance(raw_keys, list):
            kv_obj["keys"] = raw_keys
        raw_iam = raw_key_vault.get("iam_groups")
        if isinstance(raw_iam, list) and raw_iam:
            kv_iam = [g for g in raw_iam if isinstance(g, str) and g.strip()]
            if kv_iam:
                out["keyvault_iam_groups"] = kv_iam
                kv_obj["iam_groups"] = kv_iam
        out["key_vault"] = kv_obj
    return out


def get_current_user_object_id():
    """Return the Azure AD object ID of the signed-in user (for role assignment)."""
    ok, out = run_az(["ad", "signed-in-user", "show", "--query", "id", "-o", "tsv"])
    if not ok:
        fail("Could not get signed-in user. Run 'az login'.")
    return (out or "").strip()


def get_current_user_group_ids(user_object_id):
    """Return set of Azure AD group object IDs the current user is a member of."""
    ok, out = run_az(
        [
            "ad", "user", "get-member-groups",
            "--id", user_object_id,
            "--query", "[].id",
            "-o", "tsv",
        ],
        check=False,
    )
    if not ok or not (out or "").strip():
        return set()
    return set((out or "").strip().split())


def ensure_subscription(subscription_id):
    """Set the active Azure subscription."""
    ok, _ = run_az(["account", "set", "--subscription", subscription_id], check=False)
    if not ok:
        fail(
            f"Failed to set subscription to {subscription_id}. Check 'az account list' and access."
        )


# -----------------------------------------------------------------------------
# Resource group
# -----------------------------------------------------------------------------
# Ensure the Key Vault resource group exists.


def ensure_resource_group(rg_name, location, subscription_id):
    """Create resource group if it does not exist."""
    ok, _ = run_az(
        ["group", "create", "--name", rg_name, "--location", location, "--subscription", subscription_id],
        check=False,
    )
    if not ok:
        fail(f"Failed to create or update resource group '{rg_name}'.")
    msg(f"Resource group '{rg_name}' is ready.", COLOR_GREEN)


def get_keyvault_provider_state(subscription_id):
    """
    Return the registration state of Microsoft.KeyVault in the subscription.
    Returns 'Registered', 'Registering', or 'NotRegistered' (or None if check failed).
    """
    ok, out = run_az(
        [
            "provider", "show",
            "--namespace", "Microsoft.KeyVault",
            "--subscription", subscription_id,
            "--query", "registrationState",
            "-o", "tsv",
        ],
        check=False,
    )
    if not ok or not (out or "").strip():
        return None
    return (out or "").strip()


def ensure_keyvault_provider_registered(subscription_id, skip_confirm=False):
    """
    Check Microsoft.KeyVault provider registration. If not registered, start registration,
    then stop the script with a warning to run again in a couple minutes.
    If already registered, prompt user to confirm before continuing (unless skip_confirm).
    """
    state = get_keyvault_provider_state(subscription_id)
    if state is None:
        msg_stderr(
            "WARNING : Could not read Microsoft.KeyVault provider state; continuing anyway.",
            COLOR_ORANGE,
        )
        return

    state_lower = state.lower()
    if state_lower == "registered":
        msg("Microsoft.KeyVault provider is registered.", COLOR_GREEN)
        return

    if state_lower == "registering":
        msg(
            "Microsoft.KeyVault provider is still registering. Please run this script again in a couple minutes.",
            COLOR_ORANGE,
        )
        fail(
            "Exiting. Wait 1–2 minutes, then run: python create_keyvault.py"
        )

    # NotRegistered (or any other state): inform user and ask for confirmation before registering
    msg(
        "Microsoft.KeyVault provider is not registered for this subscription.",
        COLOR_ORANGE,
    )
    if not skip_confirm:
        try:
            answer = input("Register the Key Vault provider now? [y/N]: ").strip().lower() or "n"
        except EOFError:
            answer = "n"
        if answer not in ("y", "yes"):
            fail("Exiting. Run the script again when you want to register the provider.")
    ok, _ = run_az(
        [
            "provider", "register",
            "--namespace", "Microsoft.KeyVault",
            "--subscription", subscription_id,
        ],
        check=False,
    )
    if not ok:
        fail(
            "Failed to start Microsoft.KeyVault provider registration. "
            "You may need to register it manually: az provider register --namespace Microsoft.KeyVault --subscription <id>"
        )
    msg(
        "Key Vault provider registration was started (1–2 minutes). Run this script again in a couple minutes.",
        COLOR_ORANGE,
    )
    fail("Exiting. Run again in a couple minutes: python create_keyvault.py")


# -----------------------------------------------------------------------------
# Key Vault: name check, then create
# -----------------------------------------------------------------------------
# Check name availability, create vault with RBAC, wait for visibility.


def get_keyvault_resource_group(name, subscription_id):
    """Return the resource group of the Key Vault if it exists in this subscription, else None."""
    ok, out = run_az(
        [
            "keyvault", "show",
            "--name", name,
            "--subscription", subscription_id,
            "--query", "resourceGroup",
            "-o", "tsv",
        ],
        check=False,
    )
    if ok and (out or "").strip():
        return out.strip()
    return None


def check_keyvault_name_available(name, rg_name, subscription_id):
    """
    Check that the vault name is available before create (after provider is registered).
    Returns (True, None) if available, (False, other_rg) if vault exists in another RG (use it, don't fail).
    Calls fail() only if the name is in soft-delete. Call only when vault not in rg_name.
    """
    other_rg = get_keyvault_resource_group(name, subscription_id)
    if other_rg:
        return (False, other_rg)
    # Check soft-deleted vaults: name cannot be reused until purged.
    ok, out = run_az(
        [
            "keyvault", "list-deleted",
            "--subscription", subscription_id,
            "--query", f"[?name=='{name}'].name | [0]",
            "-o", "tsv",
        ],
        check=False,
    )
    if ok and (out or "").strip():
        fail(
            f"Key Vault name '{name}' is in soft-delete and cannot be reused yet. "
            f"Purge it with: az keyvault purge --name {name} --subscription {subscription_id} "
            "(after the retention period), or choose a different key_vault_name in your stack config."
        )
    msg(f"Key Vault name '{name}' is available.", COLOR_GREEN)
    return (True, None)


def keyvault_exists_in_rg(name, rg_name, subscription_id):
    """Return True only if a vault with this name exists in this resource group."""
    ok, out = run_az(
        [
            "keyvault", "list",
            "--resource-group", rg_name,
            "--subscription", subscription_id,
            "--query", f"[?name=='{name}'].id | [0]",
            "-o", "tsv",
        ],
        check=False,
    )
    return ok and bool((out or "").strip())


def wait_for_keyvault_visible(name, rg_name, subscription_id):
    """
    After create, wait until the vault appears in list (Azure can take a few seconds).
    Returns True if visible within VAULT_POLL_MAX_SECONDS, False otherwise.
    """
    elapsed = 0
    while elapsed < VAULT_POLL_MAX_SECONDS:
        if keyvault_exists_in_rg(name, rg_name, subscription_id):
            return True
        time.sleep(VAULT_POLL_INTERVAL)
        elapsed += VAULT_POLL_INTERVAL
        msg(f"  Waiting for Key Vault to be visible... ({elapsed}s)", COLOR_CYAN)
    return False


def create_key_vault(name, rg_name, location, subscription_id):
    """
    Ensure a Key Vault exists: reuse if already in our RG, reuse if same name in another RG, else create.

    Creation uses RBAC authorization. Returns the resource group name where the vault actually lives.
    """
    # Fast path: vault already in the RG we would create from rg_prefix-KV.
    if keyvault_exists_in_rg(name, rg_name, subscription_id):
        msg(f"Key Vault '{name}' already exists in {rg_name}.", COLOR_GREEN)
        return rg_name

    # Name is globally unique: either free to create, or taken in another RG (acceptable — use that vault).
    available, existing_rg = check_keyvault_name_available(name, rg_name, subscription_id)
    if not available and existing_rg:
        msg(f"Key Vault '{name}' already exists in {existing_rg}; using it.", COLOR_GREEN)
        return existing_rg

    # Create in our standard RG and poll until list/show can see it (Azure eventual consistency).
    msg(f"Creating Key Vault '{name}' in {rg_name}...", COLOR_ORANGE)
    ok, stdout, stderr = run_az_capture(
        [
            "keyvault", "create",
            "--name", name,
            "--resource-group", rg_name,
            "--location", location,
            "--subscription", subscription_id,
            "--enable-rbac-authorization", "true",
        ],
    )
    if not ok:
        msg_stderr(f"Key Vault create failed for '{name}'.", COLOR_RED)
        if stderr:
            msg_stderr(stderr, COLOR_RED)
        if stdout:
            msg_stderr(stdout, COLOR_RED)
        fail(
            f"Failed to create Key Vault '{name}'. "
            "Check that the name is globally unique, you have Contributor on the resource group, and (if the name was used before) that any soft-deleted vault with this name is purged: az keyvault list-deleted / az keyvault purge."
        )
    if not wait_for_keyvault_visible(name, rg_name, subscription_id):
        msg(
            f"Key Vault '{name}' create command returned success but vault was not visible in {rg_name} within {VAULT_POLL_MAX_SECONDS}s.",
            COLOR_ORANGE,
        )
        msg("Running diagnostics...", COLOR_ORANGE)
        show_ok, show_out, show_err = run_az_capture(
            ["keyvault", "show", "--name", name, "--subscription", subscription_id],
        )
        list_ok, list_out, list_err = run_az_capture(
            ["keyvault", "list", "--resource-group", rg_name, "--subscription", subscription_id, "-o", "table"],
        )
        for line in (show_err, show_out, list_err, list_out):
            if line:
                msg_stderr(line, COLOR_ORANGE)
        msg(
            "Check the Azure portal and run the script again if the vault appears later, or use a different key_vault_name.",
            COLOR_ORANGE,
        )
        fail(
            "Exiting. If the create actually failed (e.g. VaultAlreadyExists), fix the issue above and re-run."
        )
    msg(f"Key Vault '{name}' created with RBAC.", COLOR_GREEN)
    return rg_name


def vault_resource_id(name, subscription_id, rg_name=None):
    """
    Resolve the vault's Azure resource ID for RBAC scope.

    Order: keyvault show (subscription) → show with RG → resource list by type → synthetic path from rg_name.
    """
    ok, out = run_az(
        [
            "keyvault", "show",
            "--name", name,
            "--subscription", subscription_id,
            "--query", "id",
            "-o", "tsv",
        ],
        check=False,
    )
    if ok and (out or "").strip():
        return out.strip()
    if rg_name:
        ok, out = run_az(
            [
                "keyvault", "show",
                "--name", name,
                "--resource-group", rg_name,
                "--subscription", subscription_id,
                "--query", "id",
                "-o", "tsv",
            ],
            check=False,
        )
        if ok and (out or "").strip():
            return out.strip()
    ok, out = run_az(
        [
            "resource", "list",
            "--resource-type", "Microsoft.KeyVault/vaults",
            "--subscription", subscription_id,
            "--query", f"[?name=='{name}'].id | [0]",
            "-o", "tsv",
        ],
        check=False,
    )
    if ok and (out or "").strip():
        return out.strip()
    if rg_name:
        return f"/subscriptions/{subscription_id}/resourceGroups/{rg_name}/providers/Microsoft.KeyVault/vaults/{name}"
    fail(
        f"Could not get resource ID for Key Vault '{name}'. "
        "Ensure the vault exists in this subscription and you have read access."
    )


# -----------------------------------------------------------------------------
# IAM: check and assign
# -----------------------------------------------------------------------------
# When key_vault.iam_groups is set and non-empty: ensure signed-in user is in one of the groups
# and assign Key Vault Administrator to each group on the vault.
# When iam_groups is unset or empty: assign Key Vault Administrator to the signed-in user so
# secrets can be created (same operator identity as Azure CLI / DefaultAzureCredential).


def role_assigned_on_vault(vault_id, assignee_object_id, role_name, subscription_id):
    """
    Return True if the given principal (user or group object ID) already has the role on the vault scope.
    Includes inherited assignments (e.g. from subscription or resource group) so we don't re-assign.
    """
    ok, out = run_az(
        [
            "role", "assignment", "list",
            "--scope", vault_id,
            "--assignee-object-id", assignee_object_id,
            "--include-inherited",
            "--subscription", subscription_id,
            "--query", f"[?roleDefinitionName=='{role_name}'].id | [0]",
            "-o", "tsv",
        ],
        check=False,
    )
    return ok and bool((out or "").strip())


def verify_role_exists_in_subscription(role_name, subscription_id):
    """Verify the built-in role exists so we can assign it."""
    ok, out = run_az(
        [
            "role", "definition", "list",
            "--name", role_name,
            "--subscription", subscription_id,
            "--query", "[0].name",
            "-o", "tsv",
        ],
        check=False,
    )
    if not ok or not (out or "").strip():
        fail(
            f"Role '{role_name}' not found in subscription. "
            "Key Vault uses Azure RBAC; ensure you are in a subscription where Key Vault RBAC roles are available."
        )
    msg(f"Role '{role_name}' is available in subscription.", COLOR_GREEN)


def assign_keyvault_role_to_principal_if_missing(vault_id, assignee_object_id, role_name, subscription_id, principal_label="principal"):
    """
    Assign role to a principal (user or group) on the vault if not already assigned.
    Returns True if the role was assigned in this run; False if already had it.
    """
    if role_assigned_on_vault(vault_id, assignee_object_id, role_name, subscription_id):
        return False
    ok, _ = run_az(
        [
            "role", "assignment", "create",
            "--role", role_name,
            "--scope", vault_id,
            "--assignee-object-id", assignee_object_id,
        ],
        check=False,
    )
    if not ok:
        fail(
            f"Failed to assign '{role_name}' to {principal_label}. "
            "Ensure you have 'User Access Administrator' or 'Owner' on the subscription or resource group."
        )
    return True


def assign_keyvault_role_if_missing(vault_id, assignee_object_id, subscription_id):
    """
    Assign Key Vault Administrator to the signed-in user if missing on the vault scope.

    Not used by main() when IAM is group-driven (keyvault_iam_groups); kept for ad-hoc tooling.
    Returns True if a new assignment was made (caller may sleep for propagation).
    """
    verify_role_exists_in_subscription(KEYVAULT_ROLE_FOR_USER, subscription_id)
    if role_assigned_on_vault(vault_id, assignee_object_id, KEYVAULT_ROLE_FOR_USER, subscription_id):
        msg(f"Current user already has '{KEYVAULT_ROLE_FOR_USER}' on the Key Vault.", COLOR_GREEN)
        return False

    msg(f"Assigning '{KEYVAULT_ROLE_FOR_USER}' to current user...", COLOR_ORANGE)
    if assign_keyvault_role_to_principal_if_missing(vault_id, assignee_object_id, KEYVAULT_ROLE_FOR_USER, subscription_id, "current user"):
        msg("Role assigned. Waiting for propagation...", COLOR_CYAN)
        time.sleep(5)
        msg(f"'{KEYVAULT_ROLE_FOR_USER}' assigned to current user.", COLOR_GREEN)
        return True
    return False


def ensure_keyvault_iam_groups(vault_id, keyvault_iam_groups, current_user_id, subscription_id):
    """
    When keyvault_iam_groups is set: verify the signed-in user is in one of the groups,
    then ensure Key Vault Administrator is assigned to each group on the vault.
    Returns True if any role was just assigned (caller may wait for propagation); False otherwise.
    """
    verify_role_exists_in_subscription(KEYVAULT_ROLE_FOR_USER, subscription_id)
    # Gate: operator must belong to at least one configured group (prevents locking everyone out).
    user_group_ids = get_current_user_group_ids(current_user_id)
    allowed_ids = set(g.strip() for g in keyvault_iam_groups if g and isinstance(g, str))
    if not allowed_ids:
        return False
    if not user_group_ids.intersection(allowed_ids):
        fail(
            "The signed-in user is not a member of any group in keyvault_iam_groups. "
            "Add your user to one of the configured groups or run with a user that is already a member."
        )
    msg("Signed-in user is a member of at least one keyvault_iam_groups entry.", COLOR_GREEN)
    role_just_assigned = False
    # Per group: skip if role already effective on vault (direct or inherited — see role_assigned_on_vault).
    for group_id in allowed_ids:
        if role_assigned_on_vault(vault_id, group_id, KEYVAULT_ROLE_FOR_USER, subscription_id):
            msg(f"Group {group_id[:8]}... already has '{KEYVAULT_ROLE_FOR_USER}' on the Key Vault.", COLOR_GREEN)
            continue
        msg(f"Assigning '{KEYVAULT_ROLE_FOR_USER}' to group {group_id[:8]}...", COLOR_ORANGE)
        if assign_keyvault_role_to_principal_if_missing(vault_id, group_id, KEYVAULT_ROLE_FOR_USER, subscription_id, f"group {group_id[:8]}..."):
            role_just_assigned = True
    if role_just_assigned:
        msg("Role(s) assigned. Waiting for propagation...", COLOR_CYAN)
        time.sleep(5)
        msg(f"'{KEYVAULT_ROLE_FOR_USER}' assigned to configured groups.", COLOR_GREEN)
    return role_just_assigned


# -----------------------------------------------------------------------------
# Secrets: check and set
# -----------------------------------------------------------------------------
# For each required secret: prompt if missing, set in vault, verify.


def secret_exists_in_vault(vault_name, secret_name, subscription_id):
    """Return True only if the secret exists and we got a valid response (not 403/404 or empty)."""
    ok, stdout, stderr = run_az_capture(
        [
            "keyvault", "secret", "show",
            "--vault-name", vault_name,
            "--name", secret_name,
            "--subscription", subscription_id,
        ],
    )
    # Require success and non-empty response (az returns JSON with id/value when secret exists).
    if not ok:
        return False
    out = (stdout or "").strip()
    return bool(out and ("id" in out or "value" in out or out.startswith("{")))


def prompt_secret(secret_name, description):
    """Prompt for a secret value and verify by re-typing."""
    print(f"\n--- {secret_name} ---")
    print(description)
    while True:
        v1 = getpass.getpass(f"Enter value for {secret_name}: ")
        v2 = getpass.getpass("Re-enter to verify: ")
        if v1 == v2 and v1:
            return v1
        if not v1:
            print("Value cannot be empty.")
        else:
            print("Values did not match. Try again.")


def set_secret(vault_name, secret_name, value, subscription_id):
    """Set a secret in the vault. On failure, prints Azure CLI error and calls fail()."""
    ok, stdout, stderr = run_az_capture(
        [
            "keyvault", "secret", "set",
            "--vault-name", vault_name,
            "--name", secret_name,
            "--value", value,
            "--subscription", subscription_id,
        ],
    )
    if not ok:
        msg_stderr(f"Failed to set secret '{secret_name}' in Key Vault '{vault_name}'.", COLOR_RED)
        if stderr:
            msg_stderr(stderr, COLOR_RED)
        if stdout:
            msg_stderr(stdout, COLOR_RED)
        fail(
            f"Check vault name, subscription, and that the current user has Key Vault Secrets Officer (or Key Vault Administrator) on '{vault_name}'."
        )
    # Verify the secret is readable (confirms it was actually created).
    if not secret_exists_in_vault(vault_name, secret_name, subscription_id):
        msg(f"Secret '{secret_name}' set command succeeded but secret is not visible in vault.", COLOR_RED)
        fail(
            "IAM may still be propagating. Wait a moment and run the script again to retry, or check the vault in the portal."
        )
    msg(f"Set secret '{secret_name}'.", COLOR_GREEN)


def compute_required_secrets(cfg: dict) -> list[tuple[str, str]]:
    """
    Determine which secrets this stack wants in Key Vault.

    Prefers config key_vault.keys (list of names or {name, description} dicts).
    Falls back to legacy REQUIRED_SECRETS when key_vault.keys is not present.
    """
    kv = cfg.get("key_vault") or {}
    raw_keys = kv.get("keys")

    out: list[tuple[str, str]] = []
    if isinstance(raw_keys, list):
        for item in raw_keys:
            if isinstance(item, str):
                name = str(item).strip()
                if not name:
                    continue
                out.append((name, f"Secret {name}"))
            elif isinstance(item, dict) and item.get("name"):
                name = str(item["name"]).strip()
                if not name:
                    continue
                desc = str(item.get("description") or f"Secret {name}")
                out.append((name, desc))
    if out:
        return out
    return list(REQUIRED_SECRETS)


def ensure_secrets(key_vault_name, subscription_id, required_secrets: list[tuple[str, str]]):
    """For each required secret: if missing, prompt and set; if present, skip (green)."""
    for secret_name, description in required_secrets:
        if secret_exists_in_vault(key_vault_name, secret_name, subscription_id):
            msg(f"Secret '{secret_name}' already exists in Key Vault.", COLOR_GREEN)
            continue
        msg(f"Secret '{secret_name}' is missing; will prompt for value.", COLOR_ORANGE)
        value = prompt_secret(secret_name, description)
        set_secret(key_vault_name, secret_name, value, subscription_id)


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
# Parse args, load config, ensure provider/RG/vault/IAM/secrets.


def main():
    """
    CLI entry: parse args, load stack YAML, then either exit after a vault existence check or run full setup.

    Full setup: subscription → provider registration → RG (if needed) → vault → IAM (groups or current user) → secrets.
    """
    parser = argparse.ArgumentParser(
        description="Create Azure Key Vault from Pulumi stack config, assign IAM, and set secrets used by __main__.py.",
    )
    parser.add_argument(
        "--stack",
        default=os.getenv("PULUMI_STACK", ""),
        help="Pulumi stack name (default: PULUMI_STACK or current pulumi stack)",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip confirmation prompt before registering the Key Vault provider",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only check whether the configured Key Vault is deploy-ready (vault exists and required secrets exist). No provider registration, role assignment, or secret creation is performed.",
    )
    args = parser.parse_args()
    if args.stack:
        os.environ["PULUMI_STACK"] = args.stack

    msg("Loading stack config...", COLOR_CYAN)
    cfg = load_stack_config()
    stack = cfg["stack"]
    kv_cfg = cfg.get("key_vault") or {}
    key_vault_name = kv_cfg.get("name")
    if not key_vault_name:
        fail("load_stack_config: key_vault.name is required in stack config.")
    location = cfg["location"]
    subscription_id = cfg["subscription_id"]
    rg_prefix = cfg["rg_prefix"]
    rg_name = f"{rg_prefix}-KV"
    keyvault_iam_groups = cfg.get("keyvault_iam_groups")
    required_secrets = compute_required_secrets(cfg)

    msg(f"Stack: {stack}", COLOR_CYAN)
    msg(f"Key Vault name: {key_vault_name}", COLOR_CYAN)
    msg(f"Location: {location}", COLOR_CYAN)
    msg(f"Subscription: {subscription_id}", COLOR_CYAN)
    msg(f"Resource group: {rg_name}", COLOR_CYAN)

    ensure_subscription(subscription_id)

    # Lightweight mode for menus/automation: no side effects except subscription/vault/secret checks.
    if args.check_only:
        vault_rg = get_keyvault_resource_group(key_vault_name, subscription_id)
        if not vault_rg:
            msg(f"Key Vault not found: {key_vault_name}", COLOR_ORANGE)
            sys.exit(1)

        missing = [
            secret_name
            for secret_name, _ in required_secrets
            if not secret_exists_in_vault(key_vault_name, secret_name, subscription_id)
        ]
        if missing:
            msg(
                "Key Vault exists but is not deploy-ready; missing required secret(s): "
                + ", ".join(missing),
                COLOR_ORANGE,
            )
            sys.exit(1)

        msg(
            f"Key Vault is deploy-ready: {key_vault_name} (resource group: {vault_rg})",
            COLOR_GREEN,
        )
        sys.exit(0)

    current_user_id = get_current_user_object_id()
    msg(f"Current user (object ID): {current_user_id}", COLOR_CYAN)

    # Provider must be Registered before create; script may exit after starting registration (user re-runs later).
    ensure_keyvault_provider_registered(subscription_id, skip_confirm=args.yes)
    # Skip creating {rg_prefix}-KV if the vault already lives under a differently named RG.
    vault_rg = get_keyvault_resource_group(key_vault_name, subscription_id)
    if vault_rg is None or vault_rg == rg_name:
        ensure_resource_group(rg_name, location, subscription_id)

    # 1) Check Key Vault; create if missing (with delay until visible). Returns the effective RG (ours or existing).
    effective_rg = create_key_vault(key_vault_name, rg_name, location, subscription_id)

    # 2) IAM: group-based when key_vault.iam_groups is set; otherwise grant the signed-in user
    # Key Vault Administrator on the vault so secret set operations succeed.
    vault_id = vault_resource_id(key_vault_name, subscription_id, effective_rg)
    if keyvault_iam_groups:
        role_just_assigned = ensure_keyvault_iam_groups(vault_id, keyvault_iam_groups, current_user_id, subscription_id)
    else:
        msg(
            "key_vault.iam_groups is not set (or empty); assigning Key Vault access to the signed-in user.",
            COLOR_CYAN,
        )
        role_just_assigned = assign_keyvault_role_if_missing(
            vault_id, current_user_id, subscription_id
        )

    # 3) If we assigned the role, wait for IAM propagation before checking secrets; then check/set secrets.
    if role_just_assigned:
        msg(f"Waiting {SECRET_CHECK_DELAY_AFTER_ROLE}s for IAM propagation before checking secrets...", COLOR_CYAN)
        time.sleep(SECRET_CHECK_DELAY_AFTER_ROLE)
    ensure_secrets(key_vault_name, subscription_id, required_secrets)

    msg("\nDone. Key Vault is ready; __main__.py can use Azure CLI identity to read these secrets.", COLOR_GREEN)


if __name__ == "__main__":
    main()
