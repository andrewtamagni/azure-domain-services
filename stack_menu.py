#!/usr/bin/env python3

# stack_menu.py — interactive helper for Azure Pulumi stacks in this family of repos.
#
# Run from the repo root (same directory as Pulumi.yaml). Requires Azure CLI login for KV / az lookups.
# Usage: python stack_menu.py
#
# Supported Pulumi projects (Pulumi.yaml top-level `name` must match a key in STACK_MENU_PROFILES or fall
# back to DEFAULT_STACK_MENU_PROFILE):
#   azure-pa-hub-network   — hub UDRs + hub NSG; Key Vault required in checklist.
#   azure-spoke-network    — spoke UDRs + nsg_rules; Key Vault not required (network-only stacks).
#   azure-domain-services  — AADDS stack; route_tables + peerings + LDAP connection helpers; Key Vault required.
#   azure-dev-vms          — VM-only stacks (no NSG/route_tables/peerings in program); Key Vault required; no network menu actions.
#   azure-prod-vms         — production VM stack profile with dedicated guided prompts.
#   azure-ai-services      — AI services stack; create/backup-focused menu and minimal guided prompts.
#   azure-vms              — same as dev-vms (generic/non-env VM repo name).
#   Any Pulumi name ending in '-vms' uses the same profile as azure-dev-vms (guided stack create, no UDR/NSG/on-prem menu).
#
# Developed by Andrew Tamagni. Portions developed with assistance from Cursor AI; reviewed by humans.

import os
import re
import sys
import copy
import json
import yaml
import builtins
import importlib
import ipaddress
import subprocess
from datetime import datetime
from typing import NoReturn, Optional, Tuple

# Optional sibling script: if missing, Key Vault create/check menu paths degrade gracefully.
CREATE_KEYVAULT_SCRIPT = os.path.join(os.path.dirname(__file__), "create_keyvault.py")
create_keyvault = importlib.import_module("create_keyvault") if os.path.isfile(CREATE_KEYVAULT_SCRIPT) else None

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

# ANSI color codes (disabled when stdout is not a terminal).
# Scheme: green=success, cyan=info, orange=warning, red=error.
COLOR_RESET = "\033[0m"
COLOR_GREEN = "\033[32m"
COLOR_CYAN = "\033[36m"
COLOR_ORANGE = "\033[33m"
COLOR_RED = "\033[31m"

# Tokens for legacy merge helpers (merge_defaults_into_config / seed_value); main flow uses Pulumi.sample.yaml.
REQUIRED_TOKEN = "__REQUIRED__"
OPTIONAL_TOKEN = "__OPTIONAL__"
SECRET_TOKEN = "__SECRET__"
CONFIG_MISSING = object()

# Per-repo stack template: copy lives beside Pulumi.yaml (different keys per project).
PULUMI_SAMPLE_FILE = "Pulumi.sample.yaml"

# Values treated as "still a sample / not configured" when comparing a stack to Pulumi.sample.yaml.
NULL_UUID = "00000000-0000-0000-0000-000000000000"
SAMPLE_ARM_SUBSCRIPTION_UUID = "00000000-0000-4000-8000-000000000001"

# Per-repo layout: `Pulumi.yaml` `name` selects behavior. Copy this script into other repos; add a row
# here if you introduce a new project name. Main differences are NSG config key (`nsg_rules` vs
# `hub_nsg_rules`) and route-table default templates (spoke vs full hub UDRs).
#
# Fields:
#   nsg_rules_base_key     — YAML key the Pulumi program reads (e.g. __main__.py cfg.get_object).
#   nsg_rules_template     — "spoke" | "hub" | "generic" — which hard-coded NSG list to load as default.
#   route_tables_template  — "spoke" | "hub" | "generic" — default route_tables dict; also drives the
#                            add-route submenu (spoke: VnetToFw only; hub/generic: all three tables).
#   route_prefix_mode      — "spoke_prefix_only" (spoke stacks) | "spoke_then_network" (hub-style naming).
#   keyvault_required      — if False, checklist does not require key_vault (azure-spoke-network only).
#                            *-vms stacks use True so checklist + menu enforce Key Vault readiness (no hub NSG/UDR in program).
#   NSG menu wording       — derived from nsg_rules_template ("spoke" / "hub" / "generic"); not stored per profile.
#   show_peering_and_routes_menu — "Add peering (and routes)" on main menus (network repos only).
#   show_nsg_rule_menu            — add-one NSG action + NSG submenu in set-vars (network repos only).
#   show_add_route_table_rule_menu — "Add route table route" on main menus + route_tables submenu in set-vars.
#   show_ldap_connection_menu      — domain-services-only helper to append ldap_connections + matching NSG rule.

DEFAULT_STACK_MENU_PROFILE: dict = {
    "nsg_rules_base_key": "hub_nsg_rules",
    "nsg_rules_template": "generic",
    "route_tables_template": "generic",
    "route_prefix_mode": "spoke_then_network",
    "keyvault_required": True,
    "show_peering_and_routes_menu": True,
    "show_nsg_rule_menu": True,
    "show_add_route_table_rule_menu": False,
    "show_ldap_connection_menu": False,
}

STACK_MENU_PROFILES: dict[str, dict] = {
    "azure-spoke-network": {
        "nsg_rules_base_key": "nsg_rules",
        "nsg_rules_template": "spoke",
        "route_tables_template": "spoke",
        "route_prefix_mode": "spoke_prefix_only",
        "keyvault_required": False,
        "show_peering_and_routes_menu": True,
        "show_nsg_rule_menu": True,
        "show_add_route_table_rule_menu": True,
        "show_ldap_connection_menu": False,
    },
    "azure-domain-services": {
        "nsg_rules_base_key": "aadds_nsg_rules",
        "nsg_rules_template": "generic",
        "route_tables_template": "generic",
        "route_prefix_mode": "spoke_then_network",
        "keyvault_required": True,
        "show_peering_and_routes_menu": False,
        "show_nsg_rule_menu": False,
        "show_add_route_table_rule_menu": False,
        "show_ldap_connection_menu": True,
    },
    "azure-dev-vms": {
        # VM program does not read NSG or route_tables; generic profile avoids hub/spoke template builders.
        "nsg_rules_base_key": "nsg_rules",
        "nsg_rules_template": "generic",
        "route_tables_template": "generic",
        "route_prefix_mode": "spoke_then_network",
        "keyvault_required": True,
        "show_peering_and_routes_menu": False,
        "show_nsg_rule_menu": False,
        "show_add_route_table_rule_menu": False,
        "show_ldap_connection_menu": False,
    },
    # Same behavior as azure-dev-vms (alternate Pulumi project name for VM repos).
    "azure-vms": {
        # VM program does not read NSG or route_tables; generic profile avoids hub/spoke template builders.
        "nsg_rules_base_key": "nsg_rules",
        "nsg_rules_template": "generic",
        "route_tables_template": "generic",
        "route_prefix_mode": "spoke_then_network",
        "keyvault_required": True,
        "show_peering_and_routes_menu": False,
        "show_nsg_rule_menu": False,
        "show_add_route_table_rule_menu": False,
        "show_ldap_connection_menu": False,
    },
    "azure-prod-vms": {
        # VM program does not read NSG or route_tables; generic profile avoids hub/spoke template builders.
        "nsg_rules_base_key": "nsg_rules",
        "nsg_rules_template": "generic",
        "route_tables_template": "generic",
        "route_prefix_mode": "spoke_then_network",
        "keyvault_required": True,
        "show_peering_and_routes_menu": False,
        "show_nsg_rule_menu": False,
        "show_add_route_table_rule_menu": False,
        "show_ldap_connection_menu": False,
    },
    "azure-ai-services": {
        "nsg_rules_base_key": "nsg_rules",
        "nsg_rules_template": "generic",
        "route_tables_template": "generic",
        "route_prefix_mode": "spoke_then_network",
        "keyvault_required": False,
        "show_peering_and_routes_menu": False,
        "show_nsg_rule_menu": False,
        "show_add_route_table_rule_menu": False,
        "show_ldap_connection_menu": False,
    },
    "azure-pa-hub-network": {
        "nsg_rules_base_key": "hub_nsg_rules",
        "nsg_rules_template": "hub",
        "route_tables_template": "hub",
        "route_prefix_mode": "spoke_then_network",
        "keyvault_required": True,
        "show_peering_and_routes_menu": True,
        "show_nsg_rule_menu": True,
        "show_add_route_table_rule_menu": True,
        "show_ldap_connection_menu": False,
    },
}


def is_vms_stack_project(project_name: str) -> bool:
    """True for VM-only repos (Pulumi project name ends with '-vms')."""
    return str(project_name or "").endswith("-vms")


def is_create_backup_only_project(project_name: str) -> bool:
    """Projects that should only show create/backup actions in menus."""
    return project_name == "azure-ai-services"


def get_stack_menu_profile(project_name: str) -> dict:
    """Return merged stack_menu behavior for this Pulumi project name."""
    out = dict(DEFAULT_STACK_MENU_PROFILE)
    ovr = STACK_MENU_PROFILES.get(project_name)
    if ovr is None and is_vms_stack_project(project_name):
        # Any foo-vms project uses the same menu/profile behavior as azure-dev-vms.
        ovr = STACK_MENU_PROFILES.get("azure-dev-vms")
    if ovr:
        out.update(ovr)
    return out


def nsg_template_scope_word(project_name: str) -> str | None:
    """Return 'spoke' or 'hub' for menu labels when template is specific; else None for generic wording."""
    tpl = get_stack_menu_profile(project_name).get("nsg_rules_template", "generic")
    if tpl == "spoke":
        return "spoke"
    if tpl == "hub":
        return "hub"
    return None


def get_nsg_add_menu_label(project_name: str | None = None) -> str:
    """Main-menu label for adding one NSG rule (wording from nsg_rules_template)."""
    pn = project_name if project_name is not None else get_project_name()
    scope = nsg_template_scope_word(pn)
    return f"Add {scope} NSG rule" if scope else "Add NSG rule"


def get_nsg_submenu_option_labels(project_name: str | None = None) -> tuple[str, str]:
    """Labels for NSG submenu options 1 and 2 (add-one vs load defaults), from nsg_rules_template."""
    pn = project_name if project_name is not None else get_project_name()
    scope = nsg_template_scope_word(pn)
    if scope:
        return (
            f"Add individual {scope} NSG rule",
            f"Load default {scope} NSG rules template",
        )
    return ("Add individual NSG rule", "Load default NSG rules template")


def stack_pick_prompt_for_nsg_action(menu_label: str) -> str:
    """e.g. 'Add spoke NSG rule' -> 'Select stack to add spoke NSG rule:'."""
    s = menu_label.strip()
    if s.lower().startswith("add "):
        return f"Select stack to add {s[4:].strip()}:"
    return f"Select stack for {s}:"


def show_peering_and_routes_menu(project_name: str | None = None) -> bool:
    """*-vms projects omit peering/UDR editing from the main menu."""
    pn = project_name if project_name is not None else get_project_name()
    return bool(get_stack_menu_profile(pn).get("show_peering_and_routes_menu", True))


def show_nsg_rule_menu(project_name: str | None = None) -> bool:
    """*-vms projects omit NSG add + NSG submenu in set-vars."""
    pn = project_name if project_name is not None else get_project_name()
    return bool(get_stack_menu_profile(pn).get("show_nsg_rule_menu", True))


def show_add_route_table_rule_menu(project_name: str | None = None) -> bool:
    """Whether to show Add route table route on main menus and the route_tables submenu in set-vars."""
    pn = project_name if project_name is not None else get_project_name()
    return bool(get_stack_menu_profile(pn).get("show_add_route_table_rule_menu", False))


def show_ldap_connection_menu(project_name: str | None = None) -> bool:
    """Whether to show Add LDAP connection helper action (domain-services only)."""
    pn = project_name if project_name is not None else get_project_name()
    return bool(get_stack_menu_profile(pn).get("show_ldap_connection_menu", False))


def route_tables_menu_table_keys(project_name: str | None = None) -> list[str]:
    """
    Route table keys offered in route_tables_add_route_submenu.

    Spoke __main__.py only attaches subnet to VnetToFw; hub deploys all three route tables.
    """
    pn = project_name if project_name is not None else get_project_name()
    if get_stack_menu_profile(pn).get("route_tables_template") == "spoke":
        return ["VnetToFw"]
    return ["VnetToFw", "FwToOutbound", "FwToOnPrem_VNETs"]


def run_route_table_rule_menu_for_stack(stack: dict) -> None:
    """Append a route via route_tables_add_route_submenu for the repo's route_tables config key."""
    pn = get_project_name()
    route_tables_add_route_submenu(stack["stack_file"], f"{pn}:route_tables")


# -----------------------------------------------------------------------------
# Embedded default templates (lists/dicts merged into stack YAML)
# -----------------------------------------------------------------------------
# These mirror each repo's Pulumi.sample.yaml style. Builders pick spoke vs hub via STACK_MENU_PROFILES.

# --- Spoke NSG + routes (azure-spoke-network template) ---
SPOKE_DEFAULT_NSG_RULES = [
    {
        "name": "Allow-Outside-From-IP",
        "description": "Rule",
        "protocol": "*",
        "source_port_range": "*",
        "destination_port_range": "*",
        "source_address_prefix_ref": "on_prem_source_ip_range",
        "destination_address_prefix": "*",
        "access": "Allow",
        "priority": 100,
        "direction": "Inbound",
    },
    {
        "name": "Allow-Intra",
        "description": "Allow intra network traffic",
        "protocol": "*",
        "source_port_range": "*",
        "destination_port_range": "*",
        "source_address_prefix_ref": "vnet1_cidr",
        "destination_address_prefix": "*",
        "access": "Allow",
        "priority": 101,
        "direction": "Inbound",
    },
    {
        "name": "Default-Deny",
        "description": "Deny if we don't match Allow rule",
        "protocol": "*",
        "source_port_range": "*",
        "destination_port_range": "*",
        "source_address_prefix": "*",
        "destination_address_prefix": "*",
        "access": "Deny",
        "priority": 200,
        "direction": "Inbound",
    },
]

SPOKE_DEFAULT_ROUTE_TABLES = {
    "VnetToFw": [
        {
            "name": "SPOKE-to-FW-Route1",
            "address_prefix": "0.0.0.0/0",
            "next_hop_type": "VirtualAppliance",
            "next_hop_ip_ref": "trust_nic",
        },
        {
            "name": "SPOKE-to-FW-Route2",
            "address_prefix_ref": "on_prem_source_ip_range",
            "next_hop_type": "VirtualNetworkGateway",
        },
        {
            "name": "SPOKE-to-FW-Route3",
            "address_prefix": "192.168.0.0/16",
            "next_hop_type": "VirtualNetworkGateway",
        },
        {
            "name": "SPOKE-to-FW-Route4",
            "address_prefix": "172.16.0.0/12",
            "next_hop_type": "VirtualNetworkGateway",
        },
        {
            "name": "SPOKE-to-FW-Route5",
            "address_prefix_ref": "peerings.0.cidr",
            "next_hop_type": "VirtualAppliance",
            "next_hop_ip_ref": "trust_nic",
        },
    ],
    "FwToOutbound": [],
    "FwToOnPrem_VNETs": [],
}

# --- Hub NSG + routes (azure-pa-hub-network / *-vms hub-style template) ---
HUB_DEFAULT_HUB_NSG_RULES = [
    {
        "name": "Allow-Outside-From-IP",
        "description": "Allow from configured on-prem range ref",
        "protocol": "*",
        "source_port_range": "*",
        "destination_port_range": "*",
        "source_address_prefix_ref": "on_prem_source_ip_range",
        "destination_address_prefix": "*",
        "access": "Allow",
        "priority": 100,
        "direction": "Inbound",
    },
    {
        "name": "Allow-Intra",
        "description": "Allow intra-VNET traffic",
        "protocol": "*",
        "source_port_range": "*",
        "destination_port_range": "*",
        "source_address_prefix_ref": "vnet",
        "destination_address_prefix": "*",
        "access": "Allow",
        "priority": 101,
        "direction": "Inbound",
    },
    {
        "name": "Allow-Example-Literal-Cidr",
        "description": "Example literal source (RFC 5737 TEST-NET-3)",
        "protocol": "*",
        "source_port_range": "*",
        "destination_port_range": "*",
        "source_address_prefix": "203.0.113.0/24",
        "destination_address_prefix": "*",
        "access": "Allow",
        "priority": 102,
        "direction": "Inbound",
    },
    {
        "name": "Default-Deny-If-No-Match",
        "description": "Catch-all deny after Allow rules",
        "protocol": "*",
        "source_port_range": "*",
        "destination_port_range": "*",
        "source_address_prefix": "*",
        "destination_address_prefix": "*",
        "access": "Deny",
        "priority": 200,
        "direction": "Inbound",
    },
]

HUB_DEFAULT_ROUTE_TABLES = {
    "VnetToFw": [
        {
            "name": "SAMPLE-to-FW-DefaultRoute",
            "address_prefix": "0.0.0.0/0",
            "next_hop_type": "VirtualAppliance",
            "next_hop_ip_ref": "trust_nic",
        },
        {
            "name": "SAMPLE-to-OnPremViaGw",
            "address_prefix_ref": "on_prem_source_ip_range",
            "next_hop_type": "VirtualNetworkGateway",
        },
        {
            "name": "SAMPLE-to-SpokeA-via-FW",
            "address_prefix_ref": "peerings.0.cidr",
            "next_hop_type": "VirtualAppliance",
            "next_hop_ip_ref": "trust_nic",
        },
        {
            "name": "SAMPLE-to-SpokeB-via-FW",
            "address_prefix_ref": "peerings.1.cidr",
            "next_hop_type": "VirtualAppliance",
            "next_hop_ip_ref": "trust_nic",
        },
    ],
    "FwToOutbound": [
        {
            "name": "SAMPLE-FW-to-Internet",
            "address_prefix": "0.0.0.0/0",
            "next_hop_type": "VirtualAppliance",
            "next_hop_ip_ref": "trust_nic",
        },
        {
            "name": "SAMPLE-FW-to-OnPrem",
            "address_prefix_ref": "on_prem_source_ip_range",
            "next_hop_type": "VirtualAppliance",
            "next_hop_ip_ref": "trust_nic",
        },
        {
            "name": "SAMPLE-FW-to-SpokeA",
            "address_prefix_ref": "peerings.0.cidr",
            "next_hop_type": "VirtualAppliance",
            "next_hop_ip_ref": "trust_nic",
        },
    ],
    "FwToOnPrem_VNETs": [
        {
            "name": "SAMPLE-FW-to-Hub1",
            "address_prefix_ref": "hub1_subnet",
            "next_hop_type": "VirtualAppliance",
            "next_hop_ip_ref": "untrust_nic",
        },
        {
            "name": "SAMPLE-FW-to-Hub2",
            "address_prefix_ref": "hub2_subnet",
            "next_hop_type": "VirtualAppliance",
            "next_hop_ip_ref": "untrust_nic",
        },
        {
            "name": "Untrust-to-Trust-Route1",
            "address_prefix_ref": "trust_subnet",
            "next_hop_type": "VirtualAppliance",
            "next_hop_ip_ref": "untrust_nic",
        },
        {
            "name": "SAMPLE-FW-to-SpokeA",
            "address_prefix_ref": "peerings.0.cidr",
            "next_hop_type": "VirtualAppliance",
            "next_hop_ip_ref": "untrust_nic",
        },
        {
            "name": "SAMPLE-FW-to-SpokeB",
            "address_prefix_ref": "peerings.1.cidr",
            "next_hop_type": "VirtualAppliance",
            "next_hop_ip_ref": "untrust_nic",
        },
        {
            "name": "SAMPLE-Azure-Drop",
            "address_prefix": "10.201.0.0/16",
            "next_hop_type": "None",
        },
    ],
}

# Prefixes used when auto-renaming sample route / peering / NSG names to match stack spoke_prefix / network prefix.
ROUTE_NAME_TEMPLATE_PREFIXES = ("SPOKE", "SAMPLE", "TEST")


# -----------------------------------------------------------------------------
# Console output helpers
# -----------------------------------------------------------------------------

def color_enabled() -> bool:
    """Return True if we should emit ANSI colors (when stdout is a terminal)."""
    try:
        return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    except Exception:
        return False

def msg(text: str, color_code: str | None = None) -> None:
    """Print message to stdout with optional color. If color disabled or None, print plain."""
    if color_code and color_enabled():
        print(f"{color_code}{text}{COLOR_RESET}")
    else:
        print(text)

def msg_stderr(text: str, color_code: str | None = None) -> None:
    """Print message to stderr with optional color (e.g. for error output)."""
    if color_code and color_enabled():
        print(f"{color_code}{text}{COLOR_RESET}", file=sys.stderr)
    else:
        print(text, file=sys.stderr)

def fail(text: str) -> NoReturn:
    """Print an error message to stderr and exit with status 1."""
    msg_stderr(f"ERROR : {text}", COLOR_RED)
    raise SystemExit(1)

def quit_input_detected(choice: str) -> bool:
    """Return True when user input is any quit token we accept."""
    return choice in ("q", "quit")


def input_line_or_exit(prompt: str) -> str:
    """
    Read one line: strip whitespace; 'q' or 'quit' (any case) exits silently (status 0).
    EOF (Ctrl+D) exits the same way.
    """
    try:
        line = builtins.input(prompt)
    except EOFError:
        raise SystemExit(0) from None
    s = line.strip() if isinstance(line, str) else ""
    if quit_input_detected(s.lower()):
        raise SystemExit(0)
    return s


# -----------------------------------------------------------------------------
# YAML / project / stack resolution helpers
# -----------------------------------------------------------------------------

def load_yaml_file(path: str, required: bool = True) -> dict:
    """Load a YAML file and return a dict. Exit with a clear error if invalid."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        if required:
            fail(f"Could not find required file: {path}")
        return {}
    except yaml.YAMLError as e:
        fail(f"Failed to parse YAML file {path}: {e}")

    if data is None:
        return {}
    if not isinstance(data, dict):
        fail(f"Expected YAML object at top level in {path}")
    return data

def get_project_name() -> str:
    """Read project name from Pulumi.yaml."""
    root = load_yaml_file("Pulumi.yaml")
    proj_name = root.get("name")
    if not proj_name:
        fail('Could not read project name from "Pulumi.yaml"')
    return proj_name

def keyvault_required_for_project(project_name: str) -> bool:
    """Return True when this project requires Key Vault configuration."""
    return bool(get_stack_menu_profile(project_name).get("keyvault_required", True))


def load_pulumi_sample_config(required: bool = True) -> dict:
    """Return the `config` mapping from Pulumi.sample.yaml (empty dict if missing and not required)."""
    path = PULUMI_SAMPLE_FILE
    if not os.path.isfile(path):
        if required:
            fail(f'Required template file missing: "{path}" (add it at the project root for this repo).')
        return {}
    data = load_yaml_file(path, required=True)
    cfg = data.get("config")
    if cfg is None:
        return {}
    if not isinstance(cfg, dict):
        fail(f"Expected `{path}` to contain a YAML mapping under `config`.")
    return cfg


def get_azure_cli_account() -> dict | None:
    """Return parsed `az account show` JSON, or None if Azure CLI is unavailable or not logged in."""
    try:
        result = subprocess.run(
            ["az", "account", "show", "--output", "json"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not (result.stdout or "").strip():
            return None
        return json.loads(result.stdout)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def is_placeholder_config_string(s: str) -> bool:
    """True when a string value still looks like the sample/template (nil UUID or sample subscription in an ARM ID)."""
    if not s or not isinstance(s, str):
        return True
    t = s.strip()
    if not t:
        return True
    if t == NULL_UUID:
        return True
    if SAMPLE_ARM_SUBSCRIPTION_UUID in t:
        return True
    return False


def value_contains_placeholder(val) -> bool:
    """Recursively true if any string leaf matches is_placeholder_config_string."""
    if val is None:
        return True
    if isinstance(val, str):
        return is_placeholder_config_string(val)
    if isinstance(val, dict):
        return any(value_contains_placeholder(v) for v in val.values())
    if isinstance(val, list):
        return any(value_contains_placeholder(x) for x in val)
    return False


def build_spoke_prefix(network_resource_prefix: str, location: str) -> str:
    """Derive spoke_prefix from resource prefix and Azure region (e.g. MYORG-WESTUS)."""
    p = (network_resource_prefix or "").strip()
    loc = (location or "").strip().replace(" ", "").upper()
    if not p or not loc:
        fail("network_resource_prefix and location must be non-empty to build spoke_prefix.")
    return f"{p}-{loc}"


def merge_sample_config_into_stack(stack_config: dict, sample_config: dict) -> dict:
    """
    Fill missing keys from Pulumi.sample.yaml without overwriting existing stack values.
    Dicts are merged shallowly per key; empty dict/list values are filled from the sample.
    """
    out = copy.deepcopy(stack_config)
    for k, v in sample_config.items():
        if k not in out:
            out[k] = copy.deepcopy(v)
            continue
        cur = out[k]
        if isinstance(v, dict) and isinstance(cur, dict):
            out[k] = merge_sample_config_into_stack(cur, v)
        elif isinstance(v, list) and isinstance(cur, list):
            if not cur:
                out[k] = copy.deepcopy(v)
        # else keep existing scalar / nonempty list
    return out


def hub_shape_free_config_path(path: str, project: str) -> bool:
    """
    True for paths under hub route_tables / hub_nsg_rules. Real stacks often have a different
    number or order of routes and NSG rules than Pulumi.sample.yaml (e.g. one spoke vs two in
    the sample); positional comparison to the sample would produce false INCOMPLETE results.
    """
    if project != "azure-pa-hub-network":
        return False
    for suffix in (":route_tables", ":hub_nsg_rules"):
        key = f"{project}{suffix}"
        if path == key or path.startswith(f"{key}/"):
            return True
    return False


def hub_optional_config_path(path: str, project: str) -> bool:
    """
    True for config paths that are optional in hub stacks even if present in Pulumi.sample.yaml.
    """
    if project != "azure-pa-hub-network":
        return False
    if path == f"{project}:peerings" or path.startswith(f"{project}:peerings/"):
        return True
    if path == f"{project}:bastion" or path.startswith(f"{project}:bastion/"):
        return True
    return False


def optional_key_vault_iam_groups_path(path: str, project: str) -> bool:
    """
    True for key_vault.iam_groups when absent from stack YAML. Optional everywhere:
    create_keyvault may assign the signed-in user when no groups are configured.
    """
    return path == f"{project}:key_vault/iam_groups"


def walk_placeholders_only(stack_v, path: str, must: list[str]) -> None:
    """Recurse stack config and append paths that are empty or still use sample-style placeholders."""
    if isinstance(stack_v, dict):
        for sk, sv in stack_v.items():
            p = f"{path}/{sk}" if path else sk
            walk_placeholders_only(sv, p, must)
        return
    if isinstance(stack_v, list):
        for i, item in enumerate(stack_v):
            walk_placeholders_only(item, f"{path}/{i}" if path else str(i), must)
        return
    if stack_v is None:
        must.append(path)
        return
    if isinstance(stack_v, str) and not stack_v.strip():
        must.append(path)
        return
    if value_contains_placeholder(stack_v):
        must.append(path)


def collect_incomplete_config_paths(
    stack_cfg: dict, sample_cfg: dict, project: str | None = None
) -> tuple[list[str], list[str]]:
    """
    Walk the shape and keys in sample config; report stack paths that are missing, wrong type,
    empty, or still contain placeholder strings. Returns (must_set, optional_set); optional_set is [].

    For azure-pa-hub-network:
      - route_tables and hub_nsg_rules are checked for placeholders only (shape is free),
      - peerings and bastion are optional and never required for completeness.
    Missing key_vault.iam_groups is never treated as incomplete (optional vs sample).
    """
    if project is None:
        project = get_project_name()
    must: list[str] = []

    def walk(stack_v, sample_v, path: str) -> None:
        if hub_shape_free_config_path(path, project):
            walk_placeholders_only(stack_v, path, must)
            return
        if isinstance(sample_v, dict):
            if not isinstance(stack_v, dict):
                must.append(path or "(root)")
                return
            for sk, sv in sample_v.items():
                p = f"{path}/{sk}" if path else sk
                if hub_optional_config_path(p, project):
                    continue
                if sk not in stack_v:
                    if optional_key_vault_iam_groups_path(p, project):
                        continue
                    must.append(p)
                else:
                    walk(stack_v[sk], sv, p)
            return
        if isinstance(sample_v, list):
            if not isinstance(stack_v, list):
                must.append(path or "(root)")
                return
            n = min(len(stack_v), len(sample_v))
            for i in range(n):
                p = f"{path}/{i}" if path else str(i)
                walk(stack_v[i], sample_v[i], p)
            for i in range(n, len(stack_v)):
                p = f"{path}/{i}" if path else str(i)
                walk_placeholders_only(stack_v[i], p, must)
            return
        # Sample leaf: stack must have a non-placeholder value at this path.
        if stack_v is None:
            must.append(path)
            return
        if isinstance(stack_v, str) and not stack_v.strip():
            must.append(path)
            return
        if value_contains_placeholder(stack_v):
            must.append(path)

    walk(stack_cfg, sample_cfg, "")
    return must, []


def get_current_stack() -> str:
    """
    Return the stack basename used for the local file (e.g. dev from ORG/dev).
    Uses PULUMI_STACK or pulumi stack output; falls back to a single local stack file.
    """
    stack_name = os.getenv("PULUMI_STACK")
    if stack_name:
        return stack_name.split("/", 1)[-1]

    run_result = None
    try:
        run_result = subprocess.run(
            ["pulumi", "stack"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        fail("Pulumi CLI not found. Install Pulumi or run this from the Pulumi container.")
    except subprocess.CalledProcessError:
        pass
    if run_result and run_result.stdout:
        regex_match = re.search(r"Current stack is ([^\s:]+):", run_result.stdout)
        if regex_match:
            return regex_match.group(1).split("/", 1)[-1]

    candidates = [
        f for f in os.listdir(".")
        if f.startswith("Pulumi.") and f.endswith(".yaml") and f != "Pulumi.yaml"
    ]
    if len(candidates) == 1:
        return candidates[0].replace("Pulumi.", "").replace(".yaml", "")

    fail("No stack is selected. Run 'pulumi stack select' or 'pulumi stack init' first.")

def get_stack_file_path(basename: str) -> str:
    """Return the path to the local stack config file (e.g. Pulumi.dev.yaml)."""
    return f"Pulumi.{basename}.yaml"


def fix_pulumi_stack_yaml_permissions(path: str) -> None:
    """
    After writing Pulumi.<stack>.yaml from Docker (often as root on a bind-mounted repo),
    set mode 0644 and optionally chown to HOST_UID:HOST_GID when set by docker_pulumi_shell.sh.
    Silently ignores failures (e.g. non-root, read-only FS).
    """
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass
    uid_s = (os.environ.get("HOST_UID") or "").strip()
    gid_s = (os.environ.get("HOST_GID") or "").strip()
    if uid_s.isdigit() and gid_s.isdigit():
        try:
            os.chown(path, int(uid_s), int(gid_s))
        except OSError:
            pass


def apply_project_namespace(defaults_map: dict, project_name: str) -> dict:
    """Apply project namespace to keys that do not contain ':'."""
    out = {}
    for k, v in defaults_map.items():
        out[k if ":" in k else f"{project_name}:{k}"] = v
    return out

# -----------------------------------------------------------------------------
# default_vars merge helpers (seed default config into stack config)
# -----------------------------------------------------------------------------

def seed_value(default_value, existing_value, path_parts: list, report: dict):
    """
    Merge one default_vars branch with existing stack config.

    Returns (output_value, should_write). If the stack already has a value, we keep it and
    note "already_set". __REQUIRED__/__OPTIONAL__/__SECRET__ placeholders become report entries.
    """
    config_path = "/".join(path_parts)

    # Branch: stack already defines this path — merge nested dicts or keep as-is.
    if existing_value is not CONFIG_MISSING:
        if isinstance(default_value, dict) and isinstance(existing_value, dict):
            seeded = {}
            for key, value in default_value.items():
                child_existing = existing_value.get(key, CONFIG_MISSING)
                child_value, should_write = seed_value(
                    value,
                    child_existing,
                    path_parts + [key],
                    report,
                )
                if should_write:
                    seeded[key] = child_value

            for key, value in existing_value.items():
                if key not in seeded:
                    seeded[key] = copy.deepcopy(value)

            return seeded, True

        report["already_set"].append(config_path)
        return copy.deepcopy(existing_value), True

    # No existing value: seed from default tree, or record placeholder tokens for the UI.
    if isinstance(default_value, dict):
        seeded = {}
        for key, value in default_value.items():
            child_value, should_write = seed_value(
                value,
                CONFIG_MISSING,
                path_parts + [key],
                report,
            )
            if should_write:
                seeded[key] = child_value

        if seeded:
            return seeded, True
        return None, False

    if default_value == REQUIRED_TOKEN:
        report["must_set"].append(config_path)
        return None, False

    if default_value == OPTIONAL_TOKEN:
        report["optional_set"].append(config_path)
        return None, False

    if default_value == SECRET_TOKEN:
        report["secret_set"].append(config_path)
        return None, False

    return copy.deepcopy(default_value), True

def merge_defaults_into_config(defaults_map: dict, stack_config: object, project_name: str) -> tuple[dict, dict]:
    """
    Produce a new config mapping by applying default_vars on top of the stack's current config.

    Keys in defaults get project prefix when needed; keys only present in the stack file are preserved
    at the end. The report drives orange/cyan lists in the menu (must_set, optional_set, etc.).
    """
    # stack_config is `object` so runtime YAML (non-dict) is possible; type checkers would treat a `dict` param as always dict and mark the guard unreachable.
    if not isinstance(stack_config, dict):
        fail("Stack file 'config' section must be a YAML mapping.")

    namespaced = apply_project_namespace(defaults_map, project_name)
    report = {
        "must_set": [],
        "optional_set": [],
        "secret_set": [],
        "already_set": [],
    }
    merged = {}

    # First pass: every default key — seed or skip based on existing stack values.
    for key, value in namespaced.items():
        current = stack_config.get(key, CONFIG_MISSING)
        out_val, write = seed_value(value, current, [key], report)
        if write:
            merged[key] = out_val

    # Second pass: carry forward stack-only keys (e.g. secrets or keys not in default_vars).
    for key, value in stack_config.items():
        if key not in merged:
            merged[key] = copy.deepcopy(value)

    return merged, report

def emit_config_key_list(keys: list[str], project_name: str, color_code: str | None = None) -> None:
    """Print normalized keys for `pulumi config set` (optionally colorized)."""
    ns_prefix = f"{project_name}:"
    lines = []
    for k in keys:
        suffix = k[len(ns_prefix) :] if k.startswith(ns_prefix) else k
        lines.append(suffix.replace("/", "."))
    for item in sorted(lines):
        if color_code:
            msg(item, color_code)
        else:
            print(item)

# -----------------------------------------------------------------------------
# Environment detection
# -----------------------------------------------------------------------------

def detect_azure_environment(requirements_path: str = "requirements.txt") -> bool:
    """
    Detect whether this project is using an Azure Pulumi provider by scanning requirements.txt.
    Looks for pulumi-azure or pulumi-azure-native.
    """
    try:
        with open(requirements_path, "r", encoding="utf-8") as f:
            contents = f.read()
    except FileNotFoundError:
        msg_stderr(
            f"WARNING : requirements file not found at {requirements_path!r}; "
            "cannot auto-detect Azure environment.",
            COLOR_ORANGE,
        )
        return False

    has_pulumi_azure = "pulumi-azure" in contents
    has_pulumi_azure_native = "pulumi-azure-native" in contents

    if has_pulumi_azure or has_pulumi_azure_native:
        msg("INFO : Azure Pulumi provider detected from requirements.txt", COLOR_CYAN)
        return True

    msg(
        "WARNING : No Azure Pulumi provider (pulumi-azure or pulumi-azure-native) found in requirements.txt",
        COLOR_ORANGE,
    )
    return False


# -----------------------------------------------------------------------------
# Platform and Azure special variables
# -----------------------------------------------------------------------------
# "Special" variables are nested objects/lists in stack config (not a single pulumi config string).
# The menu injects templates via build_azure_* helpers so shapes match __main__.py.
# route_tables is handled separately: submenu appends routes instead of replacing the whole object.

PLATFORM_AZURE = "azure"

# Config keys (without project prefix) that are complex for Azure. Used to flag in UI.
SPECIAL_VARIABLES_AZURE = {
    "hub_nsg_rules",
    "nsg_rules",
    "route_tables",
    "peerings",
    "cloud_network_space",
    "vpn_gw_parameters",
    "local_gw_parameters",
    "palo_alto_vm",
    "bastion",
}

def coerce_cidr(value: str) -> str:
    """Strip CIDR text for config fields (validation happens where needed, e.g. normalize_cidr)."""
    return str(value).strip() if value is not None else ""

def coerce_ip(value: str) -> str:
    """Normalize IP or CIDR for config. Returns stripped string."""
    return str(value).strip() if value is not None else ""

def coerce_int(value) -> int:
    """Coerce to int for config (e.g. bgp_asn, priority)."""
    if isinstance(value, int):
        return value
    return int(str(value).strip()) if value not in (None, "") else 0

def coerce_bool(value) -> bool:
    """Coerce to bool for config."""
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower() if value not in (None, "") else ""
    return s in ("true", "yes", "1", "on")

def get_special_variable_base_key(config_path: str, project_name: str) -> str | None:
    """
    If config_path refers to an Azure special variable, return its base key (e.g. hub_nsg_rules).
    config_path may be 'project:key' or 'project:key/subkey'. Returns None if not special.
    """
    prefix = f"{project_name}:"
    if not config_path.startswith(prefix):
        base = config_path.split("/")[0]
        return base if base in SPECIAL_VARIABLES_AZURE else None
    rest = config_path[len(prefix) :]
    base = rest.split("/")[0].split(".")[0]
    return base if base in SPECIAL_VARIABLES_AZURE else None

def is_special_variable(config_path: str, project_name: str, platform: str = PLATFORM_AZURE) -> bool:
    """Return True if this config path is a special (complex) variable for the given platform."""
    if platform != PLATFORM_AZURE:
        return False
    return get_special_variable_base_key(config_path, project_name) is not None

def build_azure_cloud_network_space(name: str = "", cidr: str = "") -> dict:
    """Build cloud_network_space dict for Azure. Expects name (str) and cidr (str, e.g. 10.10.0.0/20)."""
    return {
        "name": str(name).strip() if name else "",
        "cidr": coerce_cidr(cidr) or "",
    }


def build_azure_bastion(name: str = "", is_allocated: bool = False) -> dict:
    """Build bastion dict for hub stacks (__main__.py): name (str), is_allocated (bool)."""
    bastion_name = str(name).strip().lower() if name else ""
    return {
        "name": bastion_name,
        "is_allocated": coerce_bool(is_allocated),
    }

def build_azure_vpn_gw_parameters(
    bgp_asn: int = 0,
    bgp_peering_address1: str = "",
    bgp_peering_address2: str = "",
) -> dict:
    """Build vpn_gw_parameters dict for Azure. BGP ASN is int; peering addresses are strings (IPs)."""
    return {
        "bgp_asn": coerce_int(bgp_asn),
        "bgp_peering_address1": coerce_ip(bgp_peering_address1),
        "bgp_peering_address2": coerce_ip(bgp_peering_address2),
    }

def build_azure_local_gw_parameters(
    connection_ip: str = "",
    bgp_asn: int = 0,
    bgp_peering_address: str = "",
) -> dict:
    """Build local_gw_parameters dict for Azure. connection_ip and bgp_peering_address are IP strings; bgp_asn is int."""
    return {
        "connection_ip": coerce_ip(connection_ip),
        "bgp_asn": coerce_int(bgp_asn),
        "bgp_peering_address": coerce_ip(bgp_peering_address),
    }

def build_azure_palo_alto_vm(vm_name: str = "", pub_ip_name: str = "", admin_username: str = "azadmin") -> dict:
    """Build palo_alto_vm dict for Azure. All string fields."""
    return {
        "vm_name": str(vm_name).strip() if vm_name else "",
        "pub_ip_name": str(pub_ip_name).strip() if pub_ip_name else "",
        "admin_username": str(admin_username).strip() if admin_username else "azadmin",
    }


def build_azure_key_vault() -> dict:
    """Default key_vault object for hub stacks (azure-pa-hub-network)."""
    return {
        "name": "",
        "keys": [
            {"name": "pavmadminpw", "description": "Palo Alto VM admin password"},
            {"name": "vpnconnectionskey", "description": "VPN connection pre-shared key"},
        ],
        "iam_groups": [],
    }


def build_azure_dev_vms_key_vault() -> dict:
    """Default key_vault object for *-vms stacks (VM admin password secret)."""
    return {
        "name": "",
        "keys": [
            {"name": "testvmadminpw", "description": "VM admin password"},
        ],
        "iam_groups": [],
    }


def build_azure_prod_vms_key_vault() -> dict:
    """Default key_vault object for azure-prod-vms (three VM admin-password secrets)."""
    return {
        "name": "",
        "keys": [
            {"name": "dc1vmadminpw", "description": "Domain controller VM admin password"},
            {"name": "adconnectvmadminpw", "description": "AD Connect VM admin password"},
            {"name": "gigvmadminpw", "description": "Fischer Identity services VM admin password"},
        ],
        "iam_groups": [],
    }


def build_azure_hub_nsg_rules() -> list:
    """
    Build default hub_nsg_rules list for Azure. Structure matches __main__.py build_hub_nsg_rules.
    Each rule: name, description, protocol, source_port_range, destination_port_range,
    source_address_prefix or source_address_prefix_ref, destination_address_prefix or destination_address_prefix_ref,
    access (Allow/Deny), priority (int), direction (Inbound/Outbound).
    """
    return [
        {
            "name": "Allow-Outside-From-IP",
            "description": "Rule",
            "protocol": "*",
            "source_port_range": "*",
            "destination_port_range": "*",
            "source_address_prefix_ref": "on_prem_source_ip_range",
            "destination_address_prefix": "*",
            "access": "Allow",
            "priority": 100,
            "direction": "Inbound",
        },
        {
            "name": "Allow-Intra",
            "description": "Allow intra network traffic",
            "protocol": "*",
            "source_port_range": "*",
            "destination_port_range": "*",
            "source_address_prefix_ref": "vnet",
            "destination_address_prefix": "*",
            "access": "Allow",
            "priority": 101,
            "direction": "Inbound",
        },
        {
            "name": "Default-Deny-If-No-Match",
            "description": "Rule",
            "protocol": "*",
            "source_port_range": "*",
            "destination_port_range": "*",
            "source_address_prefix": "*",
            "destination_address_prefix": "*",
            "access": "Deny",
            "priority": 200,
            "direction": "Inbound",
        },
    ]

def get_nsg_rules_base_key(project_name: str) -> str:
    """Return project NSG config key base name (matches Pulumi stack YAML + __main__.py)."""
    return str(get_stack_menu_profile(project_name)["nsg_rules_base_key"])


def nsg_rule_names_set(rules: list) -> set[str]:
    """Non-empty rule names in the NSG rules list (for duplicate checks)."""
    out: set[str] = set()
    for r in rules:
        if isinstance(r, dict):
            n = str(r.get("name") or "").strip()
            if n:
                out.add(n)
    return out


def suggest_unique_allow_outside_nsg_name(rules: list) -> str:
    """Next Allow-Outside-From-IP / Allow-Outside-From-IP-N not already present (hub & spoke)."""
    existing = nsg_rule_names_set(rules)
    prefix = "Allow-Outside-From-IP"
    if prefix not in existing:
        return prefix
    n = 2
    while True:
        cand = f"{prefix}-{n}"
        if cand not in existing:
            return cand
        n += 1
        if n > 100_000:
            fail("Could not find an unused default NSG rule name; rename or remove rules in stack YAML.")


def route_names_in_table(route_tables: dict, table_key: str) -> set[str]:
    names: set[str] = set()
    for r in route_tables.get(table_key) or []:
        if isinstance(r, dict):
            n = str(r.get("name") or "").strip()
            if n:
                names.add(n)
    return names


def substitute_route_template_prefix_in_name(name: str, route_prefix: str) -> str:
    """
    Replace leading SPOKE/SAMPLE/TEST (or FW-) template segments with route_prefix.
    Same rules as build_azure_route_tables_for_stack route renaming (routes, peerings, NSG names).
    """
    s = str(name or "").strip()
    if not s:
        return s
    # Keep Azure-Drop canonical so /16 derivation logic can target it reliably.
    if "Azure-Drop" in s:
        return "Azure-Drop"
    if s.startswith("FW-"):
        return f"{route_prefix}-{s}"
    for template_prefix in ROUTE_NAME_TEMPLATE_PREFIXES:
        if s.startswith(f"{template_prefix}-"):
            return f"{route_prefix}-{s[len(template_prefix) + 1 :]}"
    return s


def resolve_route_prefix_from_config(config: dict, project_name: str | None = None) -> str:
    """
    Prefix for template-based resource names: spoke_prefix and/or network_resource_prefix per profile.
    For stacks that use ``rg_prefix`` (e.g. azure-domain-services) but not spoke/network prefixes, the
    same ``rg_prefix`` value drives TEST-/SPOKE-/SAMPLE- template substitution in peerings, routes,
    and NSG rule names. Falls back to TEST if nothing is set.
    """
    pn = project_name if project_name is not None else get_project_name()
    profile = get_stack_menu_profile(pn)
    fallback_prefix = "TEST"
    try:
        spoke_prefix_key = f"{pn}:spoke_prefix"
        network_prefix_key = f"{pn}:network_resource_prefix"
        rg_prefix_key = f"{pn}:rg_prefix"
        if profile.get("route_prefix_mode") == "spoke_prefix_only":
            prefix_value = str(config.get(spoke_prefix_key) or config.get("spoke_prefix") or "").strip()
        else:
            prefix_value = str(
                config.get(spoke_prefix_key)
                or config.get("spoke_prefix")
                or config.get(network_prefix_key)
                or config.get("network_resource_prefix")
                or ""
            ).strip()
        if not prefix_value:
            prefix_value = str(
                config.get(rg_prefix_key) or config.get("rg_prefix") or ""
            ).strip()
        return prefix_value if prefix_value else fallback_prefix
    except Exception:
        return fallback_prefix


def resolve_route_prefix_for_stack(stack_file: str, config: dict | None = None) -> str:
    """Prefix for template-based names; loads stack YAML when config is not provided."""
    if config is None:
        data = load_yaml_file(stack_file, required=False)
        config = data.get("config") or {}
    return resolve_route_prefix_from_config(config)


def apply_template_prefix_to_route_tables(route_tables: dict, route_prefix: str) -> None:
    """Mutate route name fields in place (VnetToFw / FwToOutbound / FwToOnPrem_VNETs lists)."""
    if not isinstance(route_tables, dict):
        return
    for table_key, routes in route_tables.items():
        if not isinstance(routes, list):
            continue
        for r in routes:
            if isinstance(r, dict) and r.get("name") is not None:
                r["name"] = substitute_route_template_prefix_in_name(str(r["name"]), route_prefix)


def drop_peering_reference_routes(route_tables: dict) -> None:
    """Remove routes using address_prefix_ref=peerings.N.cidr (safe default when no peerings are configured)."""
    if not isinstance(route_tables, dict):
        return
    for table_key, routes in route_tables.items():
        if not isinstance(routes, list):
            continue
        route_tables[table_key] = [
            r
            for r in routes
            if not (
                isinstance(r, dict)
                and str(r.get("address_prefix_ref") or "").startswith("peerings.")
            )
        ]


def normalize_hub_peerings_defaults(config: dict, project_name: str) -> None:
    """
    Hub stacks default to no peerings; when peerings is empty, strip route entries that depend on peerings.N.cidr.
    """
    if project_name != "azure-pa-hub-network":
        return
    peer_key = f"{project_name}:peerings"
    rt_key = f"{project_name}:route_tables"
    peerings = config.get(peer_key)
    if not isinstance(peerings, list):
        peerings = []
    if peerings:
        return
    config[peer_key] = []
    rt = config.get(rt_key)
    if isinstance(rt, dict):
        drop_peering_reference_routes(rt)


def apply_template_prefixes_to_network_stack_config(config: dict, project_name: str) -> None:
    """
    After spoke stack creation (or when normalizing sample-derived config), rewrite names that use
    SPOKE/SAMPLE/TEST/FW- templates to use the resolved route prefix (peerings, route_tables, NSG rules).
    """
    route_prefix = resolve_route_prefix_from_config(config, project_name)
    peer_key = f"{project_name}:peerings"
    rt_key = f"{project_name}:route_tables"
    peerings = config.get(peer_key)
    if isinstance(peerings, list):
        for p in peerings:
            if isinstance(p, dict) and p.get("name") is not None:
                p["name"] = substitute_route_template_prefix_in_name(str(p["name"]), route_prefix)
    rt = config.get(rt_key)
    if isinstance(rt, dict):
        apply_template_prefix_to_route_tables(rt, route_prefix)
    nsg_cfg_key = f"{project_name}:{get_nsg_rules_base_key(project_name)}"
    rules = config.get(nsg_cfg_key)
    if isinstance(rules, list):
        for rule in rules:
            if isinstance(rule, dict) and rule.get("name") is not None:
                rule["name"] = substitute_route_template_prefix_in_name(str(rule["name"]), route_prefix)


def suggest_unique_route_autoname(route_tables: dict, table_key: str, stack_file: str) -> str:
    """
    Next unused route name for the table, using the same prefix as default templates:
    VnetToFw -> {prefix}-to-FW-RouteN; FwToOutbound -> {prefix}-FW-to-Outbound-RouteN;
    FwToOnPrem_VNETs -> {prefix}-FW-RouteN.
    """
    existing = route_names_in_table(route_tables, table_key)
    prefix = resolve_route_prefix_for_stack(stack_file)
    if table_key == "VnetToFw":
        stem = f"{prefix}-to-FW-Route"
    elif table_key == "FwToOutbound":
        stem = f"{prefix}-FW-to-Outbound-Route"
    elif table_key == "FwToOnPrem_VNETs":
        stem = f"{prefix}-FW-Route"
    else:
        stem = f"{table_key}-Route"
    n = 1
    while True:
        cand = f"{stem}{n}"
        if cand not in existing:
            return cand
        n += 1
        if n > 100_000:
            fail(f"Could not find an unused auto name in {table_key}; remove or rename a route.")


def build_azure_nsg_rules_for_project(project_name: str) -> list:
    """Return NSG rules defaults for the active project (profile-driven)."""
    tpl = get_stack_menu_profile(project_name).get("nsg_rules_template", "generic")
    if tpl == "spoke":
        return copy.deepcopy(SPOKE_DEFAULT_NSG_RULES)
    if tpl == "hub":
        return copy.deepcopy(HUB_DEFAULT_HUB_NSG_RULES)
    return build_azure_hub_nsg_rules()


def build_azure_nsg_rules_for_stack(stack_file: str) -> list:
    """Default NSG list with the same template-prefix substitution as route_tables (TEST/SPOKE/SAMPLE/FW-)."""
    project_name = get_project_name()
    rules = build_azure_nsg_rules_for_project(project_name)
    route_prefix = resolve_route_prefix_for_stack(stack_file)
    out = copy.deepcopy(rules)
    for rule in out:
        if isinstance(rule, dict) and rule.get("name") is not None:
            rule["name"] = substitute_route_template_prefix_in_name(str(rule["name"]), route_prefix)
    return out


def build_azure_route_tables() -> dict:
    """
    Build default route_tables dict for Azure. Structure matches __main__.py:
    VnetToFw, FwToOutbound, FwToOnPrem_VNETs. Each value is a list of route dicts.
    Each route: name, address_prefix or address_prefix_ref, next_hop_type, next_hop_ip_ref (trust_nic/untrust_nic).
    """
    return {
        "VnetToFw": [
            {"name": "TEST-to-FW-Route1", "address_prefix": "0.0.0.0/0", "next_hop_type": "VirtualAppliance", "next_hop_ip_ref": "trust_nic"},
            {"name": "TEST-to-FW-Route2", "address_prefix_ref": "on_prem_source_ip_range", "next_hop_type": "VirtualNetworkGateway"},
        ],
        "FwToOutbound": [
            {"name": "FW-to-Outbound-Route1", "address_prefix": "0.0.0.0/0", "next_hop_type": "VirtualAppliance", "next_hop_ip_ref": "trust_nic"},
            {"name": "FW-to-Outbound-Route2", "address_prefix_ref": "on_prem_source_ip_range", "next_hop_type": "VirtualAppliance", "next_hop_ip_ref": "trust_nic"},
            {"name": "FW-to-Outbound-Route3", "address_prefix_ref": "untrust_subnet", "next_hop_type": "VirtualAppliance", "next_hop_ip_ref": "trust_nic"},
        ],
        "FwToOnPrem_VNETs": [
            {"name": "FW-to-OnPrem_VNETs-Route1", "address_prefix_ref": "hub1_subnet", "next_hop_type": "VirtualAppliance", "next_hop_ip_ref": "untrust_nic"},
            {"name": "FW-to-OnPrem_VNETs-Route2", "address_prefix_ref": "hub2_subnet", "next_hop_type": "VirtualAppliance", "next_hop_ip_ref": "untrust_nic"},
            {"name": "Untrust-to-Trust-Route1", "address_prefix_ref": "trust_subnet", "next_hop_type": "VirtualAppliance", "next_hop_ip_ref": "untrust_nic"},
            {"name": "Azure-Drop", "address_prefix": "10.12.0.0/16", "next_hop_type": "None"},
        ],
    }

def build_azure_route_tables_for_stack(stack_file: str) -> dict:
    """
    Build default route tables using stack-specific values.

    - Template (spoke / hub / generic) comes from STACK_MENU_PROFILES for the project name.
    - Route names: prefix from stack YAML (`spoke_prefix` and/or `network_resource_prefix` per profile).
    - Azure-Drop CIDR is computed as /16 from the stack vnet value when a matching route exists.
    """
    project_name = get_project_name()
    profile = get_stack_menu_profile(project_name)
    rt_tpl = profile.get("route_tables_template", "generic")
    if rt_tpl == "spoke":
        tables = copy.deepcopy(SPOKE_DEFAULT_ROUTE_TABLES)
    elif rt_tpl == "hub":
        tables = copy.deepcopy(HUB_DEFAULT_ROUTE_TABLES)
    else:
        tables = copy.deepcopy(build_azure_route_tables())

    try:
        data = load_yaml_file(stack_file, required=False)
        config = data.get("config") or {}
        route_prefix = resolve_route_prefix_for_stack(stack_file, config)
        normalize_hub_peerings_defaults(config, project_name)
        if project_name == "azure-pa-hub-network":
            drop_peering_reference_routes(tables)
        apply_template_prefix_to_route_tables(tables, route_prefix)

        vnet_key = f"{project_name}:vnet"
        vnet_raw = str(config.get(vnet_key) or config.get("vnet") or "").strip()
        if vnet_raw:
            vnet_net = ipaddress.ip_network(vnet_raw, strict=False)
            azure_drop_cidr = str(vnet_net.supernet(new_prefix=16))
            for route in tables.get("FwToOnPrem_VNETs", []):
                if route.get("name") == "Azure-Drop":
                    route["address_prefix"] = azure_drop_cidr
                    break
    except Exception:
        pass

    return tables

def build_azure_peerings() -> list:
    """
    Build default peerings list for Azure. Each entry: name, remote_vnet_id, cidr (all strings).
    __main__.py expects list of dicts with those keys. Empty list by default.
    """
    return []

def normalize_cidr(cidr: str) -> str:
    """Normalize a CIDR string (e.g. '10.0.0.0/24') into canonical form."""
    try:
        return str(ipaddress.ip_network(str(cidr).strip(), strict=False))
    except Exception:
        fail(f"Invalid CIDR: {cidr!r}. Expected something like '10.0.0.0/24'.")


def normalize_route_destination_prefix(user_input: str) -> str:
    """
    Turn user input into a valid route address_prefix for stack config.

    Azure UDRs need a CIDR; '*' is accepted as shorthand for the default route 0.0.0.0/0.
    """
    raw = str(user_input).strip()
    if raw == "*":
        msg("Using 0.0.0.0/0 for route destination (same as typing the default route CIDR).", COLOR_CYAN)
        return "0.0.0.0/0"
    return normalize_cidr(raw)


# -----------------------------------------------------------------------------
# Read values from stack YAML (supports project:foo and unprefixed foo)
# -----------------------------------------------------------------------------

def get_stack_config_value(config: dict, config_key: str):
    """
    Return a config value from stack YAML.

    Pulumi stacks namespace keys with the project name (e.g. '<project>:route_tables').
    This helper also supports falling back to an unprefixed key (e.g. 'route_tables').
    """
    if config_key in config:
        return config.get(config_key)
    if ":" in config_key:
        unprefixed = config_key.split(":", 1)[1]
        return config.get(unprefixed)
    return config.get(config_key)


# -----------------------------------------------------------------------------
# Route tables: interactive append + peering-driven route rows
# -----------------------------------------------------------------------------

def route_tables_add_route_submenu(stack_file: str, route_tables_config_key: str) -> None:
    """
    Interactive submenu to append a single route to a route table (or load defaults).

    Spoke projects only list VnetToFw; hub projects list all three tables. This does NOT
    overwrite the full route_tables structure; it appends to the selected list.

    After name, destination CIDR (* → 0.0.0.0/0), and next-hop fields, shows a preview and
    asks for confirmation before writing YAML (n = discard and re-enter for the same table).
    """
    data = load_yaml_file(stack_file, required=True)
    config = data.get("config") or {}
    pn = get_project_name()
    menu_keys = route_tables_menu_table_keys(pn)

    route_tables = (
        get_stack_config_value(config, route_tables_config_key)
        or build_azure_route_tables_for_stack(stack_file)
    )
    # Ensure expected keys exist even if the config is partially filled.
    for k in menu_keys:
        if k not in route_tables or not isinstance(route_tables.get(k), list):
            route_tables[k] = []

    # Default next hop settings chosen to match the patterns already used in Pulumi.dev.yaml:
    # - Trust-side routes typically point to trust_nic
    # - Untrust-side routes typically point to untrust_nic
    next_hop_defaults = {
        "VnetToFw": {"next_hop_type": "VirtualAppliance", "next_hop_ip_ref": "trust_nic"},
        "FwToOutbound": {"next_hop_type": "VirtualAppliance", "next_hop_ip_ref": "trust_nic"},
        "FwToOnPrem_VNETs": {"next_hop_type": "VirtualAppliance", "next_hop_ip_ref": "untrust_nic"},
    }

    while True:
        if len(menu_keys) == 1:
            msg(
                "route_tables: spoke stack — only VnetToFw is deployed; add a route here or load defaults.",
                COLOR_CYAN,
            )
        else:
            msg("route_tables: hub stack — add a new route to which table?", COLOR_CYAN)
        choice_to_table: dict[int, str] = {}
        opt = 1
        for k in menu_keys:
            msg(f"  {opt}) Add route to {k}", COLOR_CYAN)
            choice_to_table[opt] = k
            opt += 1
        load_choice = opt
        msg(f"  {load_choice}) Load default route tables template", COLOR_CYAN)
        msg("  0) Back", COLOR_CYAN)
        msg("")
        try:
            raw = input_line_or_exit(f"Select an option [0-{load_choice}]: ").strip().lower()
        except EOFError:
            msg_stderr("Input closed; cancelling.", COLOR_ORANGE)
            return
        if quit_input_detected(raw):
            return
        if not raw.isdigit():
            msg("Invalid selection.", COLOR_ORANGE)
            continue
        choice = int(raw)
        if choice == 0:
            return
        if choice < 0 or choice > load_choice:
            msg("Invalid selection.", COLOR_ORANGE)
            continue
        if choice == load_choice:
            defaults = build_azure_route_tables_for_stack(stack_file)
            write_config_value_to_stack_file(stack_file, route_tables_config_key, defaults)
            msg("Loaded default route tables template into stack config.", COLOR_GREEN)
            continue

        table_key = choice_to_table[choice]

        # Build one route with next-hop + confirm so we do not write YAML until the user is done.
        while True:
            defaults = next_hop_defaults[table_key]
            auto_route_name = suggest_unique_route_autoname(route_tables, table_key, stack_file)
            try:
                route_name = input_line_or_exit(
                    f"Route name for {table_key} [blank = '{auto_route_name}']: "
                ).strip()
            except EOFError:
                msg_stderr("Input closed; cancelling.", COLOR_ORANGE)
                return

            try:
                cidr_raw = input_line_or_exit(
                    "Destination CIDR for the route (e.g. 10.0.0.0/24, or * for 0.0.0.0/0): "
                ).strip()
            except EOFError:
                msg_stderr("Input closed; cancelling.", COLOR_ORANGE)
                return
            if not cidr_raw:
                msg("CIDR cannot be empty (use * for default route 0.0.0.0/0).", COLOR_ORANGE)
                continue
            cidr = normalize_route_destination_prefix(cidr_raw)

            if not route_name:
                route_name = auto_route_name
            elif route_name in route_names_in_table(route_tables, table_key):
                fail(
                    f"Route name {route_name!r} already exists in {table_key}. "
                    "Choose another name or leave blank for an auto-generated unique name."
                )

            try:
                nth_default = defaults["next_hop_type"]
                next_hop_type_raw = input_line_or_exit(
                    f"Next hop type (Enter = {nth_default}; e.g. VirtualAppliance, VirtualNetworkGateway, None): "
                ).strip()
            except EOFError:
                msg_stderr("Input closed; cancelling.", COLOR_ORANGE)
                return
            next_hop_type = next_hop_type_raw or nth_default

            next_hop_ip_ref = None
            if next_hop_type == "VirtualAppliance":
                try:
                    ref_default = defaults["next_hop_ip_ref"]
                    next_hop_ip_ref = input_line_or_exit(
                        f"Next hop IP ref (Enter = {ref_default}; trust_nic or untrust_nic): "
                    ).strip() or ref_default
                except EOFError:
                    msg_stderr("Input closed; cancelling.", COLOR_ORANGE)
                    return

            route_entry: dict = {
                "name": route_name,
                # Literal CIDR for this flow (not address_prefix_ref).
                "address_prefix": cidr,
                "next_hop_type": next_hop_type,
            }
            if next_hop_ip_ref:
                route_entry["next_hop_ip_ref"] = next_hop_ip_ref

            msg("--- Route preview (not saved yet) ---", COLOR_CYAN)
            msg(f"  Table: {table_key}", COLOR_CYAN)
            msg(f"  name: {route_entry['name']}", COLOR_CYAN)
            msg(f"  address_prefix: {route_entry['address_prefix']}", COLOR_CYAN)
            msg(f"  next_hop_type: {route_entry['next_hop_type']}", COLOR_CYAN)
            if next_hop_ip_ref:
                msg(f"  next_hop_ip_ref: {next_hop_ip_ref}", COLOR_CYAN)
            msg("", COLOR_CYAN)

            try:
                save_raw = input_line_or_exit("Save this route to the stack file? [Y/n] (n = discard and re-enter): ").strip().lower()
            except EOFError:
                msg_stderr("Input closed; cancelling.", COLOR_ORANGE)
                return
            if save_raw == "n":
                msg("Discarded. Re-enter route details for the same table.", COLOR_ORANGE)
                continue

            route_tables[table_key].append(route_entry)
            write_config_value_to_stack_file(stack_file, route_tables_config_key, route_tables)
            msg(f"Added route '{route_name}' to {table_key}.", COLOR_GREEN)
            break

def derive_route_parts_from_peering_name(peering_name: str) -> tuple[str, str]:
    """
    Derive (local_prefix, remote_part) from a peering name.

    Example:
      peering_name = 'HUB-to-DEV-ORG-WEST-1'
      remote_part   = 'DEV-ORG-WEST-1'
      local_prefix  = 'DEV'   (first token of remote_part)
    """
    peering_name = str(peering_name).strip()
    if "to-" in peering_name:
        remote_part = peering_name.split("to-", 1)[1].strip()
        # If there are multiple 'to-' tokens, keep the last part.
        remote_part = remote_part.split("to-")[-1].strip()
    else:
        remote_part = peering_name
    local_prefix = remote_part.split("-", 1)[0] if "-" in remote_part else remote_part
    return local_prefix, remote_part

def add_peering_and_routes_to_stack(active_stack: dict) -> None:
    """
    Add a peering entry (peerings list) and append matching routes into:
      - VnetToFw
      - FwToOnPrem_VNETs

    This mirrors the example in Pulumi.dev.yaml where route_tables entries use:
      address_prefix_ref: peerings.<index>.cidr

    Note: for new peerings we only update the trust->FW and FW->on-prem route tables.
    FwToOutbound is intentionally left unchanged (not needed for new peerings).
    """
    # Steps: load YAML → validate inputs → append peering → add routes with peerings.<n>.cidr refs → write file.
    stack_file = active_stack["stack_file"]
    project_name = get_project_name()
    config_key_peerings = f"{project_name}:peerings"
    config_key_route_tables = f"{project_name}:route_tables"

    data = load_yaml_file(stack_file, required=True)
    config = data.get("config") or {}

    peerings = get_stack_config_value(config, config_key_peerings) or []
    if not isinstance(peerings, list):
        peerings = []

    route_tables = get_stack_config_value(config, config_key_route_tables) or build_azure_route_tables_for_stack(stack_file)
    # Make sure expected route-table keys exist for this project profile.
    menu_keys = route_tables_menu_table_keys(project_name)
    for k in menu_keys:
        if k not in route_tables or not isinstance(route_tables.get(k), list):
            route_tables[k] = []

    default_peering_name = f"{resolve_route_prefix_for_stack(stack_file)}-to-SPOKE"
    try:
        peering_name_raw = input_line_or_exit(
            f"Peering name (e.g. Andrew-HUB-to-SPOKE) [blank = {default_peering_name!r}]: "
        ).strip()
        peering_name = peering_name_raw if peering_name_raw else default_peering_name
        remote_vnet_id = input_line_or_exit("remote_vnet_id (full Azure resource id): ").strip()
        cidr_raw = input_line_or_exit("CIDR for the peered range (e.g. 10.100.4.0/24): ").strip()
    except EOFError:
        msg_stderr("Input closed; cancelling.", COLOR_ORANGE)
        return
    if not remote_vnet_id or not cidr_raw:
        msg("remote_vnet_id and CIDR are required.", COLOR_ORANGE)
        return
    cidr = normalize_cidr(cidr_raw)

    # Prevent duplicates by matching on (name, remote_vnet_id, cidr).
    # Do not fail if an existing entry is malformed; just skip it for the duplicate check.
    for p in peerings:
        existing_raw = p.get("cidr", "")
        if p.get("name") == peering_name and p.get("remote_vnet_id") == remote_vnet_id and existing_raw:
            try:
                existing_cidr = str(ipaddress.ip_network(str(existing_raw), strict=False))
            except Exception:
                existing_cidr = None
            if existing_cidr == cidr:
                msg("A peering with the same name/remote_vnet_id/cidr already exists. Skipping.", COLOR_CYAN)
                return

    peering_index = len(peerings)
    peering_ref = f"peerings.{peering_index}.cidr"

    # Append peering first so the new index is correct in the route refs.
    peerings.append({"name": peering_name, "remote_vnet_id": remote_vnet_id, "cidr": cidr})

    # Use the same naming strategy as other route creation flows so names are
    # consistently prefixed from stack config (e.g. network_resource_prefix).
    vnet_to_fw_name = suggest_unique_route_autoname(route_tables, "VnetToFw", stack_file)

    vnet_to_fw_ref_route = {
        "name": vnet_to_fw_name,
        "address_prefix_ref": peering_ref,
        "next_hop_type": "VirtualAppliance",
        "next_hop_ip_ref": "trust_nic",
    }
    # Only add routes if they aren't already present for this ref.
    # We check by the computed address_prefix_ref (peerings.<index>.cidr) so the
    # route logic stays consistent with the existing Pulumi.dev.yaml style.
    existing_vnet_to_fw = [
        r for r in (route_tables.get("VnetToFw") or []) if r.get("address_prefix_ref") == peering_ref
    ]
    if not existing_vnet_to_fw:
        route_tables["VnetToFw"].append(vnet_to_fw_ref_route)
    else:
        msg("VnetToFw already has a route for this peering CIDR; not adding.", COLOR_CYAN)

    if "FwToOnPrem_VNETs" in menu_keys:
        fw_to_onprem_name = suggest_unique_route_autoname(route_tables, "FwToOnPrem_VNETs", stack_file)
        fw_to_onprem_ref_route = {
            "name": fw_to_onprem_name,
            "address_prefix_ref": peering_ref,
            "next_hop_type": "VirtualAppliance",
            "next_hop_ip_ref": "untrust_nic",
        }
        existing_fw_to_onprem = [
            r for r in (route_tables.get("FwToOnPrem_VNETs") or []) if r.get("address_prefix_ref") == peering_ref
        ]
        if not existing_fw_to_onprem:
            route_tables["FwToOnPrem_VNETs"].append(fw_to_onprem_ref_route)
        else:
            msg("FwToOnPrem_VNETs already has a route for this peering CIDR; not adding.", COLOR_CYAN)

    # Write back both configs.
    config[config_key_peerings] = peerings
    config[config_key_route_tables] = route_tables
    data["config"] = config
    with open(stack_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
    fix_pulumi_stack_yaml_permissions(stack_file)
    updated_tables = ["VnetToFw"] + (["FwToOnPrem_VNETs"] if "FwToOnPrem_VNETs" in menu_keys else [])
    msg(f"Added peering and updated route tables ({', '.join(updated_tables)}).", COLOR_GREEN)


def add_domain_ldap_connection_to_stack(active_stack: dict) -> None:
    """
    Append one ldap_connections entry and a matching AADDS NSG allow rule.
    Rule naming and priority auto-increment from existing AADDS LDAPS rules.
    """
    project_name = get_project_name()
    if project_name != "azure-domain-services":
        msg("LDAP connection helper is only available for azure-domain-services.", COLOR_ORANGE)
        return

    stack_file = active_stack["stack_file"]
    conn_key = f"{project_name}:ldap_connections"
    nsg_key = f"{project_name}:aadds_nsg_rules"

    data = load_yaml_file(stack_file, required=True)
    config = data.get("config") or {}
    ldap_connections = get_stack_config_value(config, conn_key)
    if not isinstance(ldap_connections, list):
        ldap_connections = []

    aadds_nsg_rules = get_stack_config_value(config, nsg_key) or []
    if not isinstance(aadds_nsg_rules, list):
        fail(f"{nsg_key} must be a list in {stack_file}.")

    try:
        raw = input_line_or_exit("New LDAP connection source (IP or CIDR): ").strip()
    except EOFError:
        msg_stderr("Input closed; cancelling.", COLOR_ORANGE)
        return
    if not raw:
        msg("LDAP connection value cannot be empty.", COLOR_ORANGE)
        return
    try:
        if "/" in raw:
            normalized = str(ipaddress.ip_network(raw, strict=False))
        else:
            normalized = str(ipaddress.ip_address(raw))
    except ValueError:
        msg("Enter a valid IPv4/IPv6 address or CIDR.", COLOR_ORANGE)
        return
    if normalized in [str(x).strip() for x in ldap_connections]:
        msg("That LDAP connection already exists in this stack.", COLOR_ORANGE)
        return

    ldap_connections.append(normalized)
    ldap_index = len(ldap_connections) - 1

    existing_priorities: set[int] = set()
    next_rule_num = 1
    rule_num_re = re.compile(r"^Allow-LDAPS-Inbound-From-IP_(\d+)$")
    for r in aadds_nsg_rules:
        if not isinstance(r, dict):
            continue
        try:
            existing_priorities.add(int(r.get("priority")))
        except Exception:
            pass
        m = rule_num_re.match(str(r.get("name") or "").strip())
        if m:
            try:
                next_rule_num = max(next_rule_num, int(m.group(1)) + 1)
            except Exception:
                pass

    next_priority = (max(existing_priorities) + 1) if existing_priorities else 312
    while next_priority in existing_priorities:
        next_priority += 1

    new_rule = {
        "name": f"Allow-LDAPS-Inbound-From-IP_{next_rule_num}",
        "protocol": "Tcp",
        "source_port_range": "*",
        "destination_port_range": "636",
        "source_address_prefix_ref": f"ldap_connections.{ldap_index}",
        "destination_address_prefix": "*",
        "access": "Allow",
        "priority": next_priority,
        "direction": "Inbound",
    }
    aadds_nsg_rules.append(new_rule)

    write_config_value_to_stack_file(stack_file, conn_key, ldap_connections)
    write_config_value_to_stack_file(stack_file, nsg_key, aadds_nsg_rules)
    msg(
        f"Added ldap_connections[{ldap_index}] and NSG rule {new_rule['name']} (priority {next_priority}).",
        COLOR_GREEN,
    )


# -----------------------------------------------------------------------------
# NSG rules: interactive add + submenu (hub_nsg_rules / nsg_rules)
# -----------------------------------------------------------------------------

def add_hub_nsg_rule_to_stack(active_stack: dict) -> None:
    """
    Append a new rule to nsg_rules / hub_nsg_rules in the selected complete stack (profile key).

    The rule structure matches __main__.py expectations:
      - name, description
      - protocol
      - source_port_range, destination_port_range
      - source_address_prefix or source_address_prefix_ref
      - destination_address_prefix or destination_address_prefix_ref
      - access, priority, direction

    Suggests the next free priority in 100–199; blank name uses the next unused Allow-Outside-From-IP / Allow-Outside-From-IP-N
    (never one already in config). Duplicate name, duplicate priority, or failed validation exits via fail().
    Protocol defaults to '*'. Source/destination port ranges default to '*' (Azure NSG wildcard).
    Literal prefixes are checked for Azure (e.g. VirtualNetwork, not vnet); on azure-spoke-network,
    ref:vnet is rewritten to ref:vnet1_cidr.
    """
    # hub_nsg_rules is a nested list/dict, so we update the stack YAML directly
    # (instead of using `pulumi config set --path`, which is awkward for complex objects).
    stack_file = active_stack["stack_file"]
    project_name = get_project_name()
    config_key_hub = f"{project_name}:{get_nsg_rules_base_key(project_name)}"

    data = load_yaml_file(stack_file, required=True)
    config = data.get("config") or {}
    rules = get_stack_config_value(config, config_key_hub) or []
    if not isinstance(rules, list):
        rules = []
    if not rules:
        rules = build_azure_nsg_rules_for_stack(stack_file)

    # Fail before prompts if the stack already has invalid NSG rules (e.g. duplicate names).
    try:
        prepare_nsg_rules_for_stack_yaml(copy.deepcopy(rules), log_aliases=False)
    except ValueError as e:
        fail(
            "Existing NSG rules in this stack fail validation; fix them before adding another rule.\n"
            f"{e}\n"
            f"Stack file: {stack_file}\n"
            "Tip: NSG submenu option 3 validates without changing the file; edit duplicate names or priorities in YAML."
        )

    # --- Suggest next inbound Allow rule priority below the default deny (200) ---
    # Pick the smallest available priority in the "100-series" so the new rule
    # naturally stays ahead of the default deny rule (priority 200).
    existing_priorities: set[int] = set()
    for r in rules:
        pr = r.get("priority")
        try:
            existing_priorities.add(int(pr))
        except Exception:
            pass
    suggested_priority = None
    for p in range(100, 200):
        if p not in existing_priorities:
            suggested_priority = p
            break

    # Blank name -> next Allow-Outside-From-IP / Allow-Outside-From-IP-N not already in config.
    default_rule_name = suggest_unique_allow_outside_nsg_name(rules)

    if project_name == "azure-spoke-network":
        source_hint = (
            "Source (ref:on_prem_source_ip_range, ref:vnet1_cidr, CIDR, *, or tag e.g. VirtualNetwork): "
        )
        dest_hint = (
            "Destination address prefix ('*' default, or CIDR, VirtualNetwork, ref:vnet1_cidr, etc.): "
        )
    else:
        source_hint = (
            "Source (ref:on_prem_source_ip_range, ref:<config_key>, CIDR, *, or service tag e.g. VirtualNetwork): "
        )
        dest_hint = (
            "Destination address prefix ('*' default, or CIDR, service tag, or ref:<config_key>): "
        )

    try:
        # Prompts: blanks get defaults after validation (name, source, priority, etc.).
        # Name can be left blank; we'll auto-generate it after we compute priority.
        name = input_line_or_exit(
            f"NSG rule name (e.g. Allow-App-Servers) [blank = '{default_rule_name}']: "
        ).strip()
        description = input_line_or_exit("Description (blank = 'Rule'): ").strip() or "Rule"
        protocol = input_line_or_exit(
            "Protocol ('*' default, e.g. * Tcp Udp Icmp): "
        ).strip() or "*"
        source_port_range = input_line_or_exit("Source port range ('*' default, e.g. * or 80 or 80-443): ").strip() or "*"
        destination_port_range = input_line_or_exit(
            "Destination port range ('*' default, e.g. * or 443): "
        ).strip() or "*"
        priority_raw = input_line_or_exit(
            f"Priority (int, must be < 200). Suggested: {suggested_priority if suggested_priority is not None else 150}. Press Enter to use suggested: "
        ).strip()
        direction = input_line_or_exit("Direction ('Inbound'/'Outbound', default Inbound): ").strip() or "Inbound"
        access = input_line_or_exit("Access ('Allow'/'Deny', default Allow): ").strip() or "Allow"
        source_raw = input_line_or_exit(source_hint).strip()
        destination_address_raw = input_line_or_exit(dest_hint).strip() or "*"
    except EOFError:
        msg_stderr("Input closed; cancelling.", COLOR_ORANGE)
        return
    try:
        priority = int(priority_raw) if priority_raw else suggested_priority
    except Exception:
        msg("Priority must be an integer.", COLOR_ORANGE)
        return

    if priority is None or priority_raw == "" and suggested_priority is None:
        msg("No available priority in the 100-199 range; you must choose a priority < 200 manually.", COLOR_ORANGE)
        return
    if priority >= 200:
        msg("Priority must be < 200 so it stays before the default deny rule (priority 200).", COLOR_ORANGE)
        return

    # If the user left fields blank, apply defaults now.
    if not name:
        name = default_rule_name
    if not source_raw:
        # Default to allowing from the configured on-prem source IP range reference.
        source_raw = "ref:on_prem_source_ip_range"

    if not name or not source_raw:
        msg("Rule name and source are required (even after applying defaults).", COLOR_ORANGE)
        return

    existing_rule_names = nsg_rule_names_set(rules)
    if name in existing_rule_names:
        fail(f"NSG rule name {name!r} already exists in this stack. Choose a different name.")

    if priority in existing_priorities:
        fail(
            f"NSG priority {priority} is already used in this stack. "
            f"Used priorities include: {sorted(existing_priorities)}."
        )

    try:
        protocol = normalize_azure_nsg_protocol(protocol)
    except ValueError as e:
        msg(str(e), COLOR_ORANGE)
        return

    # --- Build dict: literal vs ref:* for source/destination matches __main__.py conventions ---
    route_rule = {
        "name": name,
        "description": description,
        "protocol": protocol,
        "source_port_range": source_port_range,
        "destination_port_range": destination_port_range,
        "access": access,
        "priority": priority,
        "direction": direction,
    }

    # Source address can be either a literal prefix or a reference key.
    if source_raw.startswith("ref:"):
        route_rule["source_address_prefix_ref"] = normalize_nsg_ref_key_for_project(
            source_raw.split("ref:", 1)[1], project_name
        )
    else:
        route_rule["source_address_prefix"] = source_raw

    # Destination address prefix (literal *, CIDR/IP, or ref:config_key).
    if destination_address_raw.startswith("ref:"):
        route_rule["destination_address_prefix_ref"] = normalize_nsg_ref_key_for_project(
            destination_address_raw.split("ref:", 1)[1], project_name
        )
    else:
        route_rule["destination_address_prefix"] = destination_address_raw

    try:
        if "source_address_prefix" in route_rule:
            route_rule["source_address_prefix"] = finalize_nsg_menu_literal_prefix(
                route_rule["source_address_prefix"],
                rule_name=name,
                field="source address prefix",
            )
        if "destination_address_prefix" in route_rule:
            route_rule["destination_address_prefix"] = finalize_nsg_menu_literal_prefix(
                route_rule["destination_address_prefix"],
                rule_name=name,
                field="destination address prefix",
            )
    except ValueError as e:
        msg(str(e), COLOR_ORANGE)
        return

    trial = copy.deepcopy(rules)
    trial.append(route_rule)
    try:
        prepared = prepare_nsg_rules_for_stack_yaml(trial, log_aliases=True)
    except ValueError as e:
        fail(str(e))
    write_config_value_to_stack_file(
        stack_file, config_key_hub, prepared, nsg_rules_prevalidated=True
    )
    msg(f"Added NSG rule '{name}'.", COLOR_GREEN)

def hub_nsg_rules_submenu(stack_full_name: str, stack_file: str, hub_nsg_rules_config_key: str) -> None:
    """
    Interactive submenu for nsg_rules / hub_nsg_rules:
      - add an individual NSG rule, or
      - load the default NSG rules template.
    """
    pn = get_project_name()
    add_one, load_def = get_nsg_submenu_option_labels(pn)
    base_key = get_nsg_rules_base_key(pn)
    while True:
        msg(f"{pn}:{base_key} — choose an action", COLOR_CYAN)
        msg(f"  1) {add_one}", COLOR_CYAN)
        msg(f"  2) {load_def}", COLOR_CYAN)
        msg("  3) Validate NSG rules in this stack file (no changes)", COLOR_CYAN)
        msg("  0) Back", COLOR_CYAN)
        msg("")
        try:
            raw = input_line_or_exit("Select an option [0-3]: ").strip().lower()
        except EOFError:
            msg_stderr("Input closed; cancelling.", COLOR_ORANGE)
            return
        if raw == "0" or quit_input_detected(raw):
            return
        if not raw.isdigit() or int(raw) not in (1, 2, 3):
            msg("Invalid selection.", COLOR_ORANGE)
            continue

        choice = int(raw)
        if choice == 1:
            add_hub_nsg_rule_to_stack(
                {"full_name": stack_full_name, "stack_file": stack_file}
            )
            return
        if choice == 3:
            check_nsg_rules_in_stack_file(stack_file, hub_nsg_rules_config_key)
            continue

        defaults = build_azure_nsg_rules_for_stack(stack_file)
        write_config_value_to_stack_file(stack_file, hub_nsg_rules_config_key, defaults)
        msg(f"Loaded default NSG rules template ({base_key}) into stack config.", COLOR_GREEN)
        return


# -----------------------------------------------------------------------------
# Default blobs for Azure "special" config keys (injected from menus / set-vars)
# -----------------------------------------------------------------------------

def get_azure_built_value_for_special_key(base_key: str, project_name: str, stack_file: str) -> dict | list | None:
    """Map a special variable base name to its default template (dict/list) from the build_azure_* factories."""
    if base_key in ("hub_nsg_rules", "nsg_rules"):
        return copy.deepcopy(build_azure_nsg_rules_for_stack(stack_file))
    if base_key == "route_tables":
        return copy.deepcopy(build_azure_route_tables_for_stack(stack_file))
    builders = {
        "cloud_network_space": build_azure_cloud_network_space,
        "vpn_gw_parameters": build_azure_vpn_gw_parameters,
        "local_gw_parameters": build_azure_local_gw_parameters,
        "palo_alto_vm": build_azure_palo_alto_vm,
        "peerings": build_azure_peerings,
        "bastion": build_azure_bastion,
    }
    fn = builders.get(base_key)
    return fn() if fn else None

def is_top_level_special_config_path(config_path: str, project_name: str) -> bool:
    """True if config_path is a top-level special key (no subpath), e.g. project:hub_nsg_rules."""
    if "/" in config_path:
        return False
    return get_special_variable_base_key(config_path, project_name) is not None


# -----------------------------------------------------------------------------
# Stack discovery and inspection
# -----------------------------------------------------------------------------
# discover_stacks + inspect_stack power the checklist; get_config_report drives "set missing vars".

def discover_stacks():
    """
    Discover Pulumi stacks for this project.

    Merges stacks from two sources:
      1) `pulumi stack ls --json` (backend/remote view)
      2) local Pulumi.<stack>.yaml files (local file view)
    so stacks that exist only locally are still shown in the menu.

    Returns a list of dicts with:
      - full_name: the Pulumi stack identifier (may include org/project)
      - basename: the final path segment used in Pulumi.<basename>.yaml
      - stack_file: the local stack file path
    """
    # Pulumi.sample.yaml is the stack template (not a deployable stack); basename `sample` is excluded.
    SAMPLE_STACK_BASENAME = "sample"

    stacks: list[dict] = []
    seen_basenames: set[str] = set()

    # Try Pulumi CLI first (remote/backend view).
    try:
        result = subprocess.run(
            ["pulumi", "stack", "ls", "--json"],
            check=True,
            capture_output=True,
            text=True,
        )
        data = json.loads(result.stdout or "[]")
        for entry in data:
            # entry["name"] may be "stack", "org/stack", or "org/project/stack"
            full_name = entry.get("name") or ""
            if not full_name:
                continue
            basename = full_name.split("/")[-1]
            if basename == SAMPLE_STACK_BASENAME:
                continue
            if basename in seen_basenames:
                continue
            stack_file = f"Pulumi.{basename}.yaml"
            stacks.append(
                {
                    "full_name": full_name,
                    "basename": basename,
                    "stack_file": stack_file,
                }
            )
            seen_basenames.add(basename)
    except Exception:
        pass

    # Always add local stack files as well (local view), so stacks removed from
    # backend but still present on disk remain visible/manageable.
    for fname in sorted(os.listdir(".")):
        if fname.startswith("Pulumi.") and fname.endswith(".yaml") and fname != "Pulumi.yaml":
            basename = fname.replace("Pulumi.", "").replace(".yaml", "")
            if basename == SAMPLE_STACK_BASENAME:
                continue
            if basename in seen_basenames:
                continue
            stacks.append(
                {
                    "full_name": basename,
                    "basename": basename,
                    "stack_file": fname,
                }
            )
            seen_basenames.add(basename)

    return stacks

def inspect_stack(stack_info):
    """
    Summarize one stack for the checklist using the local Pulumi.<basename>.yaml (if present).

    Returns dict with: status (complete | incomplete | remote_only), has_kv_name, kv_required, reasons,
    optional_missing. "complete" means required sample keys exist with no placeholder tokens; hub
    route_tables and hub_nsg_rules are not forced to match the sample row-for-row. Key Vault
    existence is handled separately via create_keyvault --check-only in interactive_menu.
    """
    stack_file = stack_info["stack_file"]
    reasons = []

    # If the local stack file does not exist, this may simply mean the stack is
    # managed or initialized on a different machine. Do not treat this as a
    # configuration error; just report it separately.
    if not os.path.isfile(stack_file):
        return {
            "status": "remote_only",
            "has_kv_name": False,
            "kv_required": False,
            "reasons": [f"Local stack file '{stack_file}' is not present on this machine."],
            "optional_missing": [],
        }

    # Load project name and stack config via existing helpers.
    project = get_project_name()
    kv_required = keyvault_required_for_project(project)
    stack_data = load_yaml_file(stack_file, required=False)
    config = stack_data.get("config") or {}

    # Check for key_vault.name config (required before we can create a Key Vault).
    kv_key_project = f"{project}:key_vault"
    kv_obj = config.get(kv_key_project) or config.get("key_vault")
    has_kv_name = bool(isinstance(kv_obj, dict) and kv_obj.get("name"))
    if kv_required and not has_kv_name:
        reasons.append("Missing 'key_vault.name' in stack config (required before creating Azure Key Vault).")

    # Check config against Pulumi.sample.yaml (per-repo template).
    optional_missing: list[str] = []
    try:
        sample_cfg = load_pulumi_sample_config(required=False)
        if sample_cfg:
            must_set, optional_set = collect_incomplete_config_paths(config, sample_cfg, project)
            for config_path in must_set:
                key_display = config_path.replace("/", ".")
                reasons.append(f"Missing or placeholder config (vs sample): {key_display}")
            for config_path in optional_set or []:
                key_display = config_path.replace("/", ".")
                optional_missing.append(f"Optional (not set): {key_display}")
    except Exception:
        pass

    if reasons:
        return {"status": "incomplete", "has_kv_name": has_kv_name, "kv_required": kv_required, "reasons": reasons, "optional_missing": optional_missing}
    return {"status": "complete", "has_kv_name": has_kv_name, "kv_required": kv_required, "reasons": [], "optional_missing": optional_missing}

def get_config_report(stack_file: str) -> tuple[list[str], list[str]]:
    """
    Return (must_set, optional_set) from comparing the stack file to Pulumi.sample.yaml.
    Paths use '/' for nesting. Empty lists if sample missing or stack file missing.
    Uses the same rules as collect_incomplete_config_paths (hub route/NSG lists are placeholder-only).
    """
    if not os.path.isfile(stack_file):
        return ([], [])
    try:
        stack_data = load_yaml_file(stack_file, required=False)
        config = stack_data.get("config") or {}
        sample_cfg = load_pulumi_sample_config(required=False)
        if not sample_cfg:
            return ([], [])
        must_set, optional_set = collect_incomplete_config_paths(
            config, sample_cfg, get_project_name()
        )
        return (must_set, optional_set)
    except Exception:
        return ([], [])

def get_missing_required_config(stack_file: str) -> list[str]:
    """
    Return config paths (with '/' for nesting) that are missing or placeholder vs Pulumi.sample.yaml.
    """
    must_set, _ = get_config_report(stack_file)
    return must_set

def print_stack_checklist(
    stacks: list[dict] | None = None,
    summaries: dict[str, dict] | None = None,
    kv_exists: dict[str, bool] | None = None,
    azure_env: bool = False,
) -> None:
    """
    Print a checklist of all discovered stacks and their configuration status.

    When `azure_env` is true and `kv_exists` is provided, a stack will only be shown as
    "[OK]" when `key_vault_name` is configured AND the Key Vault is deploy-ready
    (exists and required secrets are present).
    """
    if stacks is None or summaries is None:
        stacks = discover_stacks()
        summaries = {s["full_name"]: inspect_stack(s) for s in stacks}

    if not stacks:
        msg("No Pulumi stacks found for this project.", COLOR_ORANGE)
        return

    if kv_exists is None:
        kv_exists = {}

    msg("Stack checklist:", COLOR_CYAN)
    for s in stacks:
        summary = summaries[s["full_name"]]
        label = s["full_name"]
        status = summary["status"]

        has_kv_name = bool(summary.get("has_kv_name", False))
        kv_required = bool(summary.get("kv_required", True))
        kv_found = kv_exists.get(label, False)
        local_stack = os.path.isfile(s["stack_file"])

        # If the stack is otherwise complete but the Key Vault is not deploy-ready,
        # don't show it as green/OK.
        if (
            status == "complete"
            and azure_env
            and kv_required
            and has_kv_name
            and local_stack
            and not kv_found
        ):
            msg(f"  [INCOMPLETE] {label}", COLOR_ORANGE)
            msg("    - Azure Key Vault: NOT READY (missing vault and/or required secrets)", COLOR_ORANGE)
            continue

        if status == "complete":
            msg(f"  [OK] {label}", COLOR_GREEN)
        elif status == "remote_only":
            msg(f"  [REMOTE] {label}", COLOR_CYAN)
            for reason in summary["reasons"]:
                msg(f"    - {reason}", COLOR_CYAN)
        else:
            msg(f"  [INCOMPLETE] {label}", COLOR_ORANGE)
            for reason in summary["reasons"]:
                msg(f"    - {reason}", COLOR_ORANGE)
            for line in summary.get("optional_missing") or []:
                msg(f"    - {line}", COLOR_CYAN)
    msg("", COLOR_CYAN)


# -----------------------------------------------------------------------------
# Step wrappers (called from menu)
# -----------------------------------------------------------------------------
# Thin wrappers around merge/write and create_keyvault.main() with env argv manipulation.

def seed_from_pulumi_sample(stack: str | None) -> None:
    """
    Merge Pulumi.sample.yaml into the active stack file without overwriting existing keys.
    Reports paths that are still missing or placeholder vs the sample. If stack is set,
    PULUMI_STACK is used for context.
    """
    original_stack_env = os.environ.get("PULUMI_STACK")
    try:
        if stack:
            os.environ["PULUMI_STACK"] = stack
        msg(f"STEP 1 : Seeding Pulumi stack config from {PULUMI_SAMPLE_FILE}", COLOR_CYAN)
        project_name = get_project_name()
        stack_basename = get_current_stack()
        stack_path = get_stack_file_path(stack_basename)

        sample_config = load_pulumi_sample_config(required=True)

        stack_file_existed = os.path.isfile(stack_path)
        stack_data = load_yaml_file(stack_path, required=False)
        if not isinstance(stack_data, dict):
            stack_data = {}
        stack_config = stack_data.get("config") or {}
        if not isinstance(stack_config, dict):
            stack_config = {}

        merged_config = merge_sample_config_into_stack(stack_config, sample_config)
        if project_name == "azure-pa-hub-network":
            # Hub stacks do not require peerings by default; avoid seeding sample spokes.
            merged_config[f"{project_name}:peerings"] = []
            normalize_hub_peerings_defaults(merged_config, project_name)
        # Keep seeded sample-derived names consistent with stack naming conventions
        # (e.g. TEST/SAMPLE/SPOKE -> spoke_prefix or network prefix).
        apply_template_prefixes_to_network_stack_config(merged_config, project_name)
        stack_data["config"] = merged_config

        try:
            with open(stack_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(stack_data, f, default_flow_style=False, sort_keys=False)
            fix_pulumi_stack_yaml_permissions(stack_path)
        except OSError as e:
            fail(f"Failed to write {stack_path}: {e}")

        msg(f"STEP 1 : {PULUMI_SAMPLE_FILE} merged successfully.", COLOR_GREEN)
        msg("WARNING : For nested keys use: pulumi config set --path a.b.c <value>", COLOR_ORANGE)

        must_set, optional_set = collect_incomplete_config_paths(
            merged_config, sample_config, project_name
        )
        if must_set:
            msg(
                "WARNING : These paths are still missing or look like sample placeholders; fix in YAML or pulumi config set",
                COLOR_ORANGE,
            )
            emit_config_key_list(must_set, project_name, COLOR_ORANGE)

        if optional_set:
            msg("INFO : Optional keys; set when needed with 'pulumi config set'", COLOR_CYAN)
            emit_config_key_list(optional_set, project_name, COLOR_CYAN)

        msg(
            "SUCCESS : Existing config keys were preserved; only missing entries were filled from the sample.",
            COLOR_GREEN,
        )

        if stack_file_existed:
            msg(
                "INFO : Run 'pulumi preview' or 'pulumi up' to review or deploy changes.",
                COLOR_CYAN,
            )
    finally:
        if original_stack_env is not None:
            os.environ["PULUMI_STACK"] = original_stack_env
        elif "PULUMI_STACK" in os.environ:
            del os.environ["PULUMI_STACK"]

def create_az_kv(stack: str | None, yes_kv_provider: bool = False) -> None:
    """
    Create Azure Key Vault, IAM, and required secrets for the stack.

    This preserves the exact behavior of create_keyvault.py by delegating into
    its main() with a constructed argv, so no logic is duplicated here.
    """
    argv: list[str] = ["create_keyvault.py"]
    if stack:
        argv.extend(["--stack", stack])
    if yes_kv_provider:
        argv.append("--yes")

    msg("STEP 2 : Creating Azure Key Vault, IAM role, and required secrets", COLOR_CYAN)
    if create_keyvault is None:
        msg("INFO : Skipping Key Vault creation (create_keyvault.py not found in this project).", COLOR_CYAN)
        return

    old_argv = sys.argv
    try:
        sys.argv = argv
        create_keyvault.main()
        msg("STEP 2 : Azure Key Vault and secrets are ready.", COLOR_GREEN)
    finally:
        sys.argv = old_argv


def pick_stack_interactive(candidates: list[dict], prompt: str) -> dict:
    """Pick a stack dict from candidates; if only one exists, return it without prompting."""
    if not candidates:
        fail("No eligible stacks found for this action.")
    if len(candidates) == 1:
        return candidates[0]
    msg(prompt, COLOR_CYAN)
    for i, s in enumerate(candidates, start=1):
        msg(f"  {i}) {s['full_name']}", COLOR_CYAN)
    msg("  q) quit", COLOR_CYAN)
    msg("")
    try:
        raw = input_line_or_exit(f"Select stack [1-{len(candidates)}]: ").strip().lower()
    except EOFError:
        msg_stderr("Input closed; exiting.", COLOR_ORANGE)
        raise SystemExit(0)
    if quit_input_detected(raw):
        raise SystemExit(0)
    if not raw.isdigit() or not (1 <= int(raw) <= len(candidates)):
        msg("Invalid selection.", COLOR_ORANGE)
        return pick_stack_interactive(candidates, prompt)
    return candidates[int(raw) - 1]


def eligible_stacks_for_keyvault_create(
    stacks: list[dict],
    summaries: dict[str, dict],
    kv_exists: dict[str, bool],
    kv_done_stacks: set[str],
    *,
    require_complete_config: bool,
) -> list[dict]:
    """
    Stacks that may run create_keyvault: local YAML present, Key Vault required for the project,
    key_vault.name set, preflight says vault is not deploy-ready, not already created this session.

    When require_complete_config is True, the stack must also be complete vs the sample (Menu A).
    When False, incomplete stacks are included so Key Vault can be created for the chosen stack
    while another stack is \"active\" in the incomplete-stack workflow (Menu B).
    """
    out: list[dict] = []
    for s in stacks:
        fn = s["full_name"]
        if not os.path.isfile(s["stack_file"]):
            continue
        summ = summaries.get(fn, {})
        if require_complete_config and summ.get("status") != "complete":
            continue
        if not summ.get("kv_required", True):
            continue
        if not summ.get("has_kv_name", False):
            continue
        if kv_exists.get(fn, False):
            continue
        if fn in kv_done_stacks:
            continue
        out.append(s)
    return out


# -----------------------------------------------------------------------------
# Next on-prem network helper
# -----------------------------------------------------------------------------

CIDR_CHOICES = ["/24", "/25", "/26", "/27", "/28", "/29"]

def stack_has_cloud_network_space_key(stack_file: str) -> bool:
    """Return True if the stack config has cloud_network_space set (any value). Used to show on-prem menu option."""
    if not os.path.isfile(stack_file):
        return False
    try:
        project = get_project_name()
        data = load_yaml_file(stack_file, required=False)
        config = data.get("config") or {}
        key = f"{project}:cloud_network_space"
        return (key in config) or ("cloud_network_space" in config)
    except Exception:
        return False

def get_cloud_network_space(stack_file: str) -> dict | None:
    """Return the stack's cloud_network_space config (name, cidr), or None if missing/invalid."""
    project = get_project_name()
    data = load_yaml_file(stack_file, required=False)
    config = data.get("config") or {}
    key = f"{project}:cloud_network_space"
    value = config.get(key) or config.get("cloud_network_space")
    if isinstance(value, dict) and value.get("name") and value.get("cidr"):
        return {"name": str(value["name"]).strip(), "cidr": str(value["cidr"]).strip()}
    return None

def run_next_onprem_net(stack_full_name: str, cidr: str) -> None:
    """Run get_next_onprem_net.py with the given stack and CIDR mask (stack supplies cloud_network_space)."""
    script = "get_next_onprem_net.py"
    if not os.path.isfile(script):
        fail(f"Required script not found: {script}")
    msg(f"INFO : Checking next available on-prem network for stack {stack_full_name}, mask {cidr}", COLOR_CYAN)
    env = os.environ.copy()
    env["PULUMI_STACK"] = stack_full_name
    result = subprocess.run(
        [sys.executable, script, cidr, "--stack", stack_full_name],
        env=env,
        cwd=os.getcwd(),
        capture_output=False,
    )
    if result.returncode != 0:
        msg_stderr(f"Script {script} exited with code {result.returncode}.", COLOR_ORANGE)

def run_check_next_onprem_network() -> None:
    """Prompt for stack and CIDR; use stack's cloud_network_space, then run get_next_onprem_net.py."""
    stacks = discover_stacks()
    # Only stacks that have a local config file and have cloud_network_space (name, cidr) set.
    local_stacks = [
        s for s in stacks
        if os.path.isfile(s["stack_file"]) and get_cloud_network_space(s["stack_file"])
    ]
    if not local_stacks:
        msg("No stack has cloud_network_space (name, cidr) set. Set it in a stack config to use this option.", COLOR_ORANGE)
        return

    if len(local_stacks) == 1:
        chosen_stack = local_stacks[0]
        msg(f"Using stack: {chosen_stack['full_name']}", COLOR_CYAN)
    else:
        msg("Select a stack:", COLOR_CYAN)
        for i, s in enumerate(local_stacks, start=1):
            msg(f"  {i}) {s['full_name']}", COLOR_CYAN)
        msg("")
        try:
            raw = input_line_or_exit(f"Stack number [1-{len(local_stacks)}]: ").strip()
        except EOFError:
            msg_stderr("Input closed; cancelling.", COLOR_ORANGE)
            return
        if not raw.isdigit() or int(raw) < 1 or int(raw) > len(local_stacks):
            msg("Invalid selection.", COLOR_ORANGE)
            return
        chosen_stack = local_stacks[int(raw) - 1]
    full_name = chosen_stack["full_name"]

    msg(f"Enter CIDR mask for the range (e.g. /24, /28). Choices: {', '.join(CIDR_CHOICES)}", COLOR_CYAN)
    while True:
        try:
            cidr_raw = input_line_or_exit("CIDR [/28]: ").strip().lower() or "/28"
        except EOFError:
            msg_stderr("Input closed; cancelling.", COLOR_ORANGE)
            return
        if quit_input_detected(cidr_raw):
            raise SystemExit(0)
        if cidr_raw not in CIDR_CHOICES:
            # Accept bare numbers: 24 -> /24, 28 -> /28, etc.
            if cidr_raw.isdigit() and f"/{cidr_raw}" in CIDR_CHOICES:
                cidr_raw = f"/{cidr_raw}"
            else:
                msg(f"Invalid CIDR. Use one of: {', '.join(CIDR_CHOICES)}", COLOR_ORANGE)
                continue
        break
    run_next_onprem_net(full_name, cidr_raw)

    msg("", COLOR_CYAN)
    try:
        choice = input_line_or_exit("Press Enter to return to menu, or q to quit: ").strip().lower()
    except EOFError:
        return
    if quit_input_detected(choice):
        raise SystemExit(0)


# -----------------------------------------------------------------------------
# Stack backup helper
# -----------------------------------------------------------------------------

def export_stack_backup(stack_full_name: str, stack_basename: str) -> None:
    """
    Export one Pulumi stack state to a timestamped local JSON file.

    Filename format: <stack>-backup-YYYY-MM-DD-HHMM.json
    """
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    backup_file = f"{stack_basename}-backup-{timestamp}.json"
    msg(
        f"INFO : Exporting stack '{stack_full_name}' to '{backup_file}'",
        COLOR_CYAN,
    )
    try:
        subprocess.run(
            ["pulumi", "stack", "export", "--stack", stack_full_name, "--file", backup_file],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        msg_stderr("Pulumi CLI was not found in PATH.", COLOR_RED)
        return
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        stdout = (e.stdout or "").strip()
        if stderr:
            msg_stderr(stderr, COLOR_ORANGE)
        elif stdout:
            msg_stderr(stdout, COLOR_ORANGE)
        msg_stderr(f"Failed to backup stack '{stack_full_name}'.", COLOR_RED)
        return

    msg(f"SUCCESS : Stack backup written to '{backup_file}'.", COLOR_GREEN)


def run_backup_stack() -> None:
    """Prompt for stack (when needed), then export stack state to a local backup file."""
    stacks = discover_stacks()
    if not stacks:
        msg("No Pulumi stacks found for this project.", COLOR_ORANGE)
        return

    stacks_sorted = sorted(stacks, key=lambda s: s["full_name"])
    if len(stacks_sorted) == 1:
        chosen = stacks_sorted[0]
    else:
        msg("Select stack to backup:", COLOR_CYAN)
        for i, s in enumerate(stacks_sorted, start=1):
            msg(f"  {i}) {s['full_name']}", COLOR_CYAN)
        msg("  0) back to menu", COLOR_CYAN)
        msg("")

        while True:
            try:
                raw = builtins.input(f"Stack number [0-{len(stacks_sorted)}]: ").strip()
            except EOFError:
                msg_stderr("Input closed; returning to menu.", COLOR_ORANGE)
                return
            if not raw:
                msg("Invalid selection.", COLOR_ORANGE)
                continue
            if raw == "0":
                return
            if quit_input_detected(raw.lower()):
                raise SystemExit(0)
            if not raw.isdigit():
                msg("Invalid selection.", COLOR_ORANGE)
                continue
            idx = int(raw)
            if 1 <= idx <= len(stacks_sorted):
                chosen = stacks_sorted[idx - 1]
                break
            msg("Invalid selection.", COLOR_ORANGE)

    export_stack_backup(chosen["full_name"], chosen["basename"])


# -----------------------------------------------------------------------------
# Hub bastion toggle helper
# -----------------------------------------------------------------------------

def update_bastion_for_stack(active_stack: dict) -> None:
    """Add or remove (de-allocate) hub bastion by updating only project:bastion in stack YAML."""
    project_name = get_project_name()
    if project_name != "azure-pa-hub-network":
        msg("Bastion menu is only available for azure-pa-hub-network.", COLOR_ORANGE)
        return

    stack_full_name = active_stack["full_name"]
    stack_file = active_stack["stack_file"]
    if not os.path.isfile(stack_file):
        msg(f"Local stack file not found: {stack_file}", COLOR_ORANGE)
        return

    bastion_key = f"{project_name}:bastion"
    data = load_yaml_file(stack_file, required=True)
    config = data.get("config") or {}
    existing = get_stack_config_value(config, bastion_key)

    existing_name = ""
    is_allocated = False
    if isinstance(existing, dict):
        existing_name = str(existing.get("name") or "").strip()
        is_allocated = coerce_bool(existing.get("is_allocated"))

    status = "ALLOCATED" if is_allocated else "NOT ALLOCATED"
    msg(f"Bastion status for {stack_full_name}: {status}", COLOR_CYAN)
    if existing_name:
        msg(f"Current bastion name: {existing_name}", COLOR_CYAN)
    msg("  1) Add bastion host", COLOR_CYAN)
    msg("  2) Remove bastion host", COLOR_CYAN)
    msg("  0) back to menu", COLOR_CYAN)
    msg("  q) quit", COLOR_CYAN)
    msg("")

    while True:
        raw = input_line_or_exit("Select an option [0-2]: ").strip().lower()
        if raw == "0":
            return
        if raw not in ("1", "2"):
            msg("Invalid selection.", COLOR_ORANGE)
            continue
        break

    if raw == "1":
        default_name = existing_name or f"{active_stack['basename']}-hub-bastion"
        bastion_name = prompt_line_required(
            "Bastion host name",
            f"{bastion_key}.name",
            default_name,
        )
        write_config_value_to_stack_file(
            stack_file,
            bastion_key,
            build_azure_bastion(name=bastion_name, is_allocated=True),
        )
        msg(f"Bastion enabled in '{stack_file}'.", COLOR_GREEN)
        return

    write_config_value_to_stack_file(
        stack_file,
        bastion_key,
        build_azure_bastion(name=existing_name, is_allocated=False),
    )
    msg(f"Bastion disabled in '{stack_file}'.", COLOR_GREEN)


def run_bastion_host_menu(active_stack: dict | None = None) -> None:
    """Select a hub stack (if needed) and open Add/Remove bastion action."""
    project_name = get_project_name()
    if project_name != "azure-pa-hub-network":
        msg("Bastion menu is only available for azure-pa-hub-network.", COLOR_ORANGE)
        return

    if active_stack is not None:
        update_bastion_for_stack(active_stack)
        return

    stacks = discover_stacks()
    local_stacks = [s for s in stacks if os.path.isfile(s["stack_file"])]
    if not local_stacks:
        msg("No local stack files found for this project.", COLOR_ORANGE)
        return

    if len(local_stacks) == 1:
        chosen = local_stacks[0]
    else:
        msg("Select stack for bastion action:", COLOR_CYAN)
        for i, s in enumerate(local_stacks, start=1):
            msg(f"  {i}) {s['full_name']}", COLOR_CYAN)
        msg("  0) back to menu", COLOR_CYAN)
        msg("")

        while True:
            raw = input_line_or_exit(f"Stack number [0-{len(local_stacks)}]: ").strip().lower()
            if raw == "0":
                return
            if not raw.isdigit():
                msg("Invalid selection.", COLOR_ORANGE)
                continue
            idx = int(raw)
            if 1 <= idx <= len(local_stacks):
                chosen = local_stacks[idx - 1]
                break
            msg("Invalid selection.", COLOR_ORANGE)

    update_bastion_for_stack(chosen)


# -----------------------------------------------------------------------------
# Azure VMs test VM allocation/public-IP helpers
# -----------------------------------------------------------------------------

AZURE_VMS_LINUX_VM_NAME = "test-linux-vm"
AZURE_VMS_WINDOWS_VM_NAME = "test-windows-vm"
AZURE_VMS_ADMIN_PASSWORD_SECRET = "testvmadminpw"


def parse_bool_text(value: str) -> bool | None:
    """Parse flexible true/false input. Returns None when text is not a recognized boolean."""
    s = str(value or "").strip().lower()
    if s in ("true", "t", "yes", "y", "1", "on"):
        return True
    if s in ("false", "f", "no", "n", "0", "off"):
        return False
    return None


def prompt_bool_line(label: str, key_display: str, current: bool) -> bool:
    """Prompt for a boolean with current value as default."""
    default_text = "true" if current else "false"
    while True:
        raw = input_line_or_exit(f"{label} ({key_display}) [{default_text}]: ").strip()
        if not raw:
            return current
        parsed = parse_bool_text(raw)
        if parsed is not None:
            return parsed
        msg("Enter true/false (accepted: true/false, yes/no, y/n, 1/0).", COLOR_ORANGE)


def build_azure_vms_os_vm_lists(config: dict, project_name: str) -> dict[str, list[dict]]:
    """
    Build normalized OS-separated VM lists for azure-vms stacks.

    Primary shape:
      - project:linux-vms
      - project:windows-vms
    Legacy keys (project:vms / project:test_vm) are read only for migration fallback.
    """
    linux_key = f"{project_name}:linux-vms"
    windows_key = f"{project_name}:windows-vms"
    legacy_vms_key = f"{project_name}:vms"
    legacy_test_vm_key = f"{project_name}:test_vm"

    def read_list(value) -> list[dict]:
        out: list[dict] = []
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    out.append(copy.deepcopy(item))
        return out

    linux_vms = read_list(config.get(linux_key))
    windows_vms = read_list(config.get(windows_key))

    # Fallback from legacy combined list if new keys are absent.
    if not linux_vms and not windows_vms:
        for vm in read_list(config.get(legacy_vms_key)):
            vm_os = str(vm.get("os_type") or "").strip().lower()
            if vm_os == "windows":
                windows_vms.append(vm)
            else:
                linux_vms.append(vm)

    # Fallback from legacy single test_vm for linux.
    if not linux_vms:
        raw_test_vm = config.get(legacy_test_vm_key)
        legacy_test_vm = None
        if isinstance(raw_test_vm, str):
            try:
                parsed = json.loads(raw_test_vm)
                if isinstance(parsed, dict):
                    legacy_test_vm = parsed
            except json.JSONDecodeError:
                legacy_test_vm = None
        elif isinstance(raw_test_vm, dict):
            legacy_test_vm = dict(raw_test_vm)
        if isinstance(legacy_test_vm, dict):
            linux_vms.append(legacy_test_vm)

    shared_admin_user = (
        str((linux_vms[0] if linux_vms else {}).get("admin_username") or "").strip()
        or "azadmin"
    )
    shared_admin_secret = (
        str((linux_vms[0] if linux_vms else {}).get("admin_password_secret") or "").strip()
        or AZURE_VMS_ADMIN_PASSWORD_SECRET
    )

    def normalize_entry(
        vm: dict | None,
        *,
        default_name: str,
        default_allocated: bool,
        default_pub_ip: bool,
    ) -> dict:
        src = vm if isinstance(vm, dict) else {}
        name = str(src.get("vm_name") or "").strip() or default_name
        admin_user = str(src.get("admin_username") or "").strip() or shared_admin_user
        admin_secret = str(src.get("admin_password_secret") or "").strip() or shared_admin_secret
        allocated = coerce_bool(src.get("is_allocated")) if "is_allocated" in src else default_allocated
        pub_ip = coerce_bool(src.get("has_pub_ip")) if "has_pub_ip" in src else default_pub_ip
        out = copy.deepcopy(src)
        out["vm_name"] = name
        out["admin_username"] = admin_user
        out["admin_password_secret"] = admin_secret
        out["is_allocated"] = allocated
        out["has_pub_ip"] = pub_ip
        return out

    if linux_vms:
        linux_vms = [
            normalize_entry(vm, default_name=AZURE_VMS_LINUX_VM_NAME, default_allocated=True, default_pub_ip=False)
            for vm in linux_vms
        ]
    else:
        linux_vms = [
            normalize_entry(None, default_name=AZURE_VMS_LINUX_VM_NAME, default_allocated=True, default_pub_ip=False)
        ]

    if windows_vms:
        windows_vms = [
            normalize_entry(vm, default_name=AZURE_VMS_WINDOWS_VM_NAME, default_allocated=False, default_pub_ip=False)
            for vm in windows_vms
        ]
    else:
        windows_vms = [
            normalize_entry(None, default_name=AZURE_VMS_WINDOWS_VM_NAME, default_allocated=False, default_pub_ip=False)
        ]

    return {"linux": linux_vms, "windows": windows_vms}


def get_azure_vms_test_vm_status(stack_file: str, project_name: str) -> dict[str, dict] | None:
    """Return normalized status for Linux/Windows test VMs from a stack YAML file."""
    if project_name != "azure-vms":
        return None
    if not os.path.isfile(stack_file):
        return None
    data = load_yaml_file(stack_file, required=False)
    config = data.get("config") or {}
    if not isinstance(config, dict):
        return None
    entries = build_azure_vms_os_vm_lists(config, project_name)
    linux = entries["linux"][0] if entries.get("linux") else None
    windows = entries["windows"][0] if entries.get("windows") else None
    if not linux or not windows:
        return None
    return {"linux": linux, "windows": windows}


def set_azure_vms_test_vm_flags(active_stack: dict, os_type: str) -> None:
    """Set is_allocated / has_pub_ip for Linux or Windows test VM in one stack."""
    project_name = get_project_name()
    if project_name != "azure-vms":
        msg("This action is only available in the azure-vms project.", COLOR_ORANGE)
        return

    stack_file = active_stack["stack_file"]
    if not os.path.isfile(stack_file):
        msg(f"Local stack file not found: {stack_file}", COLOR_ORANGE)
        return

    data = load_yaml_file(stack_file, required=True)
    config = data.get("config") or {}
    if not isinstance(config, dict):
        config = {}

    vm_lists = build_azure_vms_os_vm_lists(config, project_name)
    target_list = vm_lists["linux"] if os_type == "linux" else vm_lists["windows"]
    target = target_list[0] if target_list else None
    if target is None:
        fail(f"Could not resolve {os_type} VM entry for stack file {stack_file}.")

    vm_name = str(target.get("vm_name") or f"test-{os_type}-vm")
    current_alloc = coerce_bool(target.get("is_allocated"))
    current_pub = coerce_bool(target.get("has_pub_ip"))

    msg(f"Current {os_type} VM status for {active_stack['full_name']}:", COLOR_CYAN)
    msg(f"  - vm_name: {vm_name}", COLOR_CYAN)
    msg(f"  - is_allocated: {str(current_alloc).lower()}", COLOR_CYAN)
    msg(f"  - has_pub_ip: {str(current_pub).lower()}", COLOR_CYAN)

    new_alloc = prompt_bool_line(
        f"Set {os_type} VM allocation",
        f"{project_name}:{os_type}-vms[0].is_allocated",
        current_alloc,
    )
    new_pub = prompt_bool_line(
        f"Set {os_type} VM public IP",
        f"{project_name}:{os_type}-vms[0].has_pub_ip",
        current_pub,
    )

    target["is_allocated"] = new_alloc
    target["has_pub_ip"] = new_pub

    linux_key = f"{project_name}:linux-vms"
    windows_key = f"{project_name}:windows-vms"
    write_config_value_to_stack_file(stack_file, linux_key, vm_lists["linux"])
    write_config_value_to_stack_file(stack_file, windows_key, vm_lists["windows"])

    msg(
        f"Updated {os_type} VM flags in '{stack_file}' (is_allocated={str(new_alloc).lower()}, has_pub_ip={str(new_pub).lower()}).",
        COLOR_GREEN,
    )


def run_set_azure_vms_test_vm_flags(os_type: str, active_stack: dict | None = None) -> None:
    """Pick a stack (if needed) and set Linux/Windows test VM allocation + public IP flags."""
    project_name = get_project_name()
    if project_name != "azure-vms":
        msg("This action is only available in the azure-vms project.", COLOR_ORANGE)
        return
    if os_type not in ("linux", "windows"):
        fail(f"Unsupported VM os_type: {os_type}")

    if active_stack is not None:
        set_azure_vms_test_vm_flags(active_stack, os_type)
        return

    stacks = discover_stacks()
    local_stacks = [s for s in stacks if os.path.isfile(s["stack_file"])]
    if not local_stacks:
        msg("No local stack files found for this project.", COLOR_ORANGE)
        return

    if len(local_stacks) == 1:
        chosen = local_stacks[0]
    else:
        msg(f"Select stack for {os_type} VM settings:", COLOR_CYAN)
        for i, s in enumerate(local_stacks, start=1):
            status = get_azure_vms_test_vm_status(s["stack_file"], project_name)
            if status:
                st = status[os_type]
                msg(
                    f"  {i}) {s['full_name']}  (is_allocated={str(coerce_bool(st.get('is_allocated'))).lower()}, has_pub_ip={str(coerce_bool(st.get('has_pub_ip'))).lower()})",
                    COLOR_CYAN,
                )
            else:
                msg(f"  {i}) {s['full_name']}", COLOR_CYAN)
        msg("  0) back to menu", COLOR_CYAN)
        msg("")

        while True:
            raw = input_line_or_exit(f"Stack number [0-{len(local_stacks)}]: ").strip().lower()
            if raw == "0":
                return
            if not raw.isdigit():
                msg("Invalid selection.", COLOR_ORANGE)
                continue
            idx = int(raw)
            if 1 <= idx <= len(local_stacks):
                chosen = local_stacks[idx - 1]
                break
            msg("Invalid selection.", COLOR_ORANGE)

    set_azure_vms_test_vm_flags(chosen, os_type)


# -----------------------------------------------------------------------------
# Set required variables (one at a time)
# -----------------------------------------------------------------------------

def run_set_required_variables(stack_full_name: str, stack_file: str) -> None:
    """
    Loop: list missing required keys (and optional keys), let user pick one to set.

    Top-level Azure special keys: write YAML templates or open route_tables submenu.
    Simple keys: run `pulumi config set` or `pulumi config set --path` with PULUMI_STACK set.
    """
    env = os.environ.copy()
    env["PULUMI_STACK"] = stack_full_name
    project_name = get_project_name()
    if project_name == "azure-pa-hub-network":
        prompt_azure_pa_hub_network_required_config(stack_file, project_name)
    elif project_name == "azure-domain-services":
        stack_data = load_yaml_file(stack_file, required=False)
        if not isinstance(stack_data, dict):
            stack_data = {}
        cfg = stack_data.get("config") or {}
        if not isinstance(cfg, dict):
            cfg = {}
        prompt_azure_domain_services_stack_config_into(cfg, project_name, is_new_stack_flow=False)
        stack_data["config"] = cfg
        with open(stack_file, "w", encoding="utf-8") as f:
            yaml.safe_dump(stack_data, f, default_flow_style=False, sort_keys=False)
        fix_pulumi_stack_yaml_permissions(stack_file)
        msg("Captured required azure-domain-services values into stack config.", COLOR_GREEN)
    elif project_name == "azure-ai-services":
        prompt_azure_ai_services_stack_required_config(stack_file, project_name)
    elif project_name == "azure-prod-vms":
        prompt_azure_prod_vms_stack_required_config(stack_file, project_name)
    elif is_vms_stack_project(project_name):
        prompt_azure_vms_stack_required_config(stack_file, project_name)

    while True:
        # Refresh from disk each iteration so prior sets take effect.
        missing, optional = get_config_report(stack_file)
        if not missing:
            msg("All required variables are already set for this stack.", COLOR_GREEN)
            return

        # Single numbered list: required rows first, then optional (for convenience).
        optional_sorted = sorted(set(optional))
        combined = [(p, True) for p in missing] + [(p, False) for p in optional_sorted]

        msg(f"Config for stack '{stack_full_name}':", COLOR_CYAN)
        n_required = len(missing)
        for i, (path, is_required) in enumerate(combined, start=1):
            if not is_required and i == n_required + 1:
                msg("  Optional (set when needed):", COLOR_CYAN)
            key_display = path.replace("/", ".")
            special_suffix = " (Azure special)" if is_special_variable(path, project_name) else ""
            color = COLOR_ORANGE if is_required else COLOR_CYAN
            msg(f"  {i}) {key_display}{special_suffix}", color)
        msg("  0) back to menu", COLOR_CYAN)
        msg("  q) quit", COLOR_CYAN)
        msg("")
        max_num = len(combined)
        try:
            raw = input_line_or_exit(f"Number to set [0-{max_num}]: ").strip().lower()
        except EOFError:
            msg_stderr("Input closed; returning to menu.", COLOR_ORANGE)
            return
        if raw == "0":
            return
        if quit_input_detected(raw):
            raise SystemExit(0)
        if not raw.isdigit():
            msg(f"Enter a number 0-{max_num} or q to quit.", COLOR_ORANGE)
            continue
        idx = int(raw)
        if idx < 1 or idx > max_num:
            msg(f"Invalid number. Use 0-{max_num} or q to quit.", COLOR_ORANGE)
            continue

        config_path, _ = combined[idx - 1]
        key_for_cmd = config_path.replace("/", ".")

        # Top-level Azure special variable: inject built structure into stack YAML.
        if is_top_level_special_config_path(config_path, project_name):
            base_key = get_special_variable_base_key(config_path, project_name)
            if base_key == "route_tables":
                if show_add_route_table_rule_menu(project_name):
                    try:
                        # route_tables is a complex object, so we provide a dedicated submenu
                        # that appends to one of the route table lists (instead of asking for
                        # the full YAML structure in one input).
                        route_tables_add_route_submenu(stack_file, config_path)
                    except Exception as e:
                        msg_stderr(f"Failed to update route_tables: {e}", COLOR_RED)
                    continue
                msg(
                    "route_tables is not edited through this menu for *-vms projects; "
                    "set or remove it in Pulumi.<stack>.yaml directly.",
                    COLOR_ORANGE,
                )
                continue
            if base_key in ("hub_nsg_rules", "nsg_rules"):
                if show_nsg_rule_menu(project_name):
                    try:
                        # hub_nsg_rules can be set one rule at a time or loaded from defaults.
                        hub_nsg_rules_submenu(stack_full_name, stack_file, config_path)
                    except Exception as e:
                        msg_stderr(f"Failed to update hub_nsg_rules: {e}", COLOR_RED)
                    continue
                msg(
                    "NSG rules are not edited through this menu for *-vms projects; "
                    "set or remove them in Pulumi.<stack>.yaml directly.",
                    COLOR_ORANGE,
                )
                continue
            if base_key == "peerings" and not show_peering_and_routes_menu(project_name):
                msg(
                    "peerings are not edited through this menu for *-vms projects; "
                    "set or remove them in Pulumi.<stack>.yaml directly.",
                    COLOR_ORANGE,
                )
                continue

            built = get_azure_built_value_for_special_key(base_key, project_name, stack_file)
            if built is not None:
                try:
                    write_config_value_to_stack_file(stack_file, config_path, built)
                    msg(f"Injected Azure template for {key_for_cmd}. Edit Pulumi stack YAML to customize.", COLOR_GREEN)
                except Exception as e:
                    msg_stderr(f"Failed to write stack file: {e}", COLOR_RED)
                continue

        # Single-value or nested leaf: prompt and use pulumi config set.
        try:
            value = input_line_or_exit(f"Value for {key_for_cmd}: ").strip()
        except EOFError:
            msg_stderr("Input closed; returning to menu.", COLOR_ORANGE)
            return
        if not value:
            msg("Value cannot be empty; skipping.", COLOR_ORANGE)
            continue

        use_path = "/" in config_path
        if use_path:
            result = subprocess.run(
                ["pulumi", "config", "set", "--path", key_for_cmd, value],
                env=env,
                cwd=os.getcwd(),
                capture_output=True,
                text=True,
            )
        else:
            result = subprocess.run(
                ["pulumi", "config", "set", key_for_cmd, value],
                env=env,
                cwd=os.getcwd(),
                capture_output=True,
                text=True,
            )
        if result.returncode != 0:
            msg_stderr(result.stderr or "pulumi config set failed.", COLOR_RED)
            continue
        msg(f"Set {key_for_cmd}.", COLOR_GREEN)

        # Re-check; if no more missing, we're done.
        missing = get_missing_required_config(stack_file)
        if not missing:
            msg("All required variables are now set for this stack.", COLOR_GREEN)
            return


# -----------------------------------------------------------------------------
# Stack creation helpers, shared line prompts, and NSG literal validation (menu + prepare_*)
# -----------------------------------------------------------------------------

def prompt_line_required(label: str, key_display: str, initial: str) -> str:
    """One line: optional default in brackets. q/quit exits via input_line_or_exit."""
    while True:
        raw = input_line_or_exit(f"{label} ({key_display}) [{initial}]: ")
        val = raw if raw else initial
        if val:
            return val
        msg("Value cannot be empty.", COLOR_ORANGE)


AZURE_VMS_VM_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")


def validate_azure_vms_vm_name(vm_name: str, os_type: str) -> str:
    """Validate azure-vms VM name input for allowed characters and OS-specific limits."""
    name = str(vm_name or "").strip()
    if not name:
        raise ValueError("VM name cannot be empty.")
    if not AZURE_VMS_VM_NAME_PATTERN.fullmatch(name):
        raise ValueError("VM name may only contain letters, numbers, and hyphens.")
    if os_type == "windows":
        if len(name) > 15:
            raise ValueError("Windows VM name must be 15 characters or fewer.")
        if name.isdigit():
            raise ValueError("Windows VM name cannot be entirely numeric.")
    return name


def prompt_azure_vms_vm_name_line(label: str, key_display: str, initial: str, os_type: str) -> str:
    """Prompt for VM name with azure-vms validation and Windows length checks."""
    while True:
        value = prompt_line_required(label, key_display, initial)
        try:
            return validate_azure_vms_vm_name(value, os_type)
        except ValueError as e:
            msg(str(e), COLOR_ORANGE)


def prompt_cidr_line(label: str, key_display: str, initial: str) -> str:
    """Like prompt_line_required but validate IPv4 CIDR notation."""
    while True:
        s = prompt_line_required(label, key_display, initial)
        try:
            ipaddress.ip_network(s, strict=False)
            return str(s).strip()
        except ValueError:
            msg(f"Not a valid CIDR: {s!r}", COLOR_ORANGE)


def prompt_ip_line(label: str, key_display: str, initial: str) -> str:
    """Like prompt_line_required but validate IPv4 address notation."""
    while True:
        s = prompt_line_required(label, key_display, initial)
        try:
            ipaddress.ip_address(s)
            return str(s).strip()
        except ValueError:
            msg(f"Not a valid IP address: {s!r}", COLOR_ORANGE)


def prompt_asn_line(label: str, key_display: str, initial: str) -> int:
    """Prompt for a positive integer BGP ASN."""
    while True:
        s = prompt_line_required(label, key_display, initial)
        try:
            n = int(str(s).strip())
            if n < 1:
                raise ValueError("ASN must be positive")
            return n
        except ValueError:
            msg(f"Not a valid ASN: {s!r} (enter a positive integer)", COLOR_ORANGE)


def prompt_azure_pa_hub_network_required_config(stack_file: str, project_name: str) -> None:
    """
    Guided required inputs for azure-pa-hub-network set-vars flow.
    This captures core naming, networking, Key Vault name, VM names, and BGP gateway parameters.
    """
    if project_name != "azure-pa-hub-network":
        return

    stack_data = load_yaml_file(stack_file, required=False)
    if not isinstance(stack_data, dict):
        stack_data = {}
    config = stack_data.get("config") or {}
    if not isinstance(config, dict):
        config = {}

    def get_top(key: str, default: str = "") -> str:
        return str(config.get(f"{project_name}:{key}") or default).strip()

    def get_nested(parent_key: str, leaf_key: str, default: str = "") -> str:
        obj = config.get(f"{project_name}:{parent_key}")
        if isinstance(obj, dict):
            return str(obj.get(leaf_key) or default).strip()
        return default

    msg("Guided required values for azure-pa-hub-network:", COLOR_CYAN)
    msg("Press Enter to keep the value shown in [brackets].", COLOR_CYAN)

    config[f"{project_name}:rg_prefix"] = prompt_line_required(
        "Resource group prefix", f"{project_name}:rg_prefix", get_top("rg_prefix", "ORG")
    )
    config[f"{project_name}:network_resource_prefix"] = prompt_line_required(
        "Network resource prefix",
        f"{project_name}:network_resource_prefix",
        get_top("network_resource_prefix", "ORG-TEST"),
    )
    config[f"{project_name}:vnet"] = prompt_cidr_line(
        "Hub VNET CIDR", f"{project_name}:vnet", get_top("vnet", "10.0.0.0/22")
    )
    config[f"{project_name}:on_prem_source_ip_range"] = prompt_cidr_line(
        "On-prem source CIDR",
        f"{project_name}:on_prem_source_ip_range",
        get_top("on_prem_source_ip_range", "10.10.0.0/16"),
    )

    key_vault_obj = config.get(f"{project_name}:key_vault")
    if not isinstance(key_vault_obj, dict):
        key_vault_obj = copy.deepcopy(build_azure_key_vault())
    key_vault_obj["name"] = prompt_line_required(
        "Key Vault name",
        f"{project_name}:key_vault.name",
        get_nested("key_vault", "name", "test-hub-kv-replace-me"),
    )
    config[f"{project_name}:key_vault"] = key_vault_obj

    palo_obj = config.get(f"{project_name}:palo_alto_vm")
    if not isinstance(palo_obj, dict):
        palo_obj = copy.deepcopy(build_azure_palo_alto_vm())
    palo_obj["pub_ip_name"] = prompt_line_required(
        "Palo Alto public IP name",
        f"{project_name}:palo_alto_vm.pub_ip_name",
        get_nested("palo_alto_vm", "pub_ip_name", "test-pan-pip"),
    )
    palo_obj["vm_name"] = prompt_line_required(
        "Palo Alto VM name",
        f"{project_name}:palo_alto_vm.vm_name",
        get_nested("palo_alto_vm", "vm_name", "test-pan-fw01"),
    )
    config[f"{project_name}:palo_alto_vm"] = palo_obj

    local_gw = config.get(f"{project_name}:local_gw_parameters")
    if not isinstance(local_gw, dict):
        local_gw = copy.deepcopy(build_azure_local_gw_parameters())
    local_gw["bgp_asn"] = prompt_asn_line(
        "Local gateway BGP ASN",
        f"{project_name}:local_gw_parameters.bgp_asn",
        get_nested("local_gw_parameters", "bgp_asn", "65001"),
    )
    local_gw["bgp_peering_address"] = prompt_ip_line(
        "Local gateway BGP peering address",
        f"{project_name}:local_gw_parameters.bgp_peering_address",
        get_nested("local_gw_parameters", "bgp_peering_address", "10.199.0.1"),
    )
    local_gw["connection_ip"] = prompt_ip_line(
        "Local gateway connection IP",
        f"{project_name}:local_gw_parameters.connection_ip",
        get_nested("local_gw_parameters", "connection_ip", "192.0.2.1"),
    )
    config[f"{project_name}:local_gw_parameters"] = local_gw

    vpn_gw = config.get(f"{project_name}:vpn_gw_parameters")
    if not isinstance(vpn_gw, dict):
        vpn_gw = copy.deepcopy(build_azure_vpn_gw_parameters())
    vpn_gw["bgp_asn"] = prompt_asn_line(
        "VPN gateway BGP ASN",
        f"{project_name}:vpn_gw_parameters.bgp_asn",
        get_nested("vpn_gw_parameters", "bgp_asn", "65515"),
    )
    vpn_gw["bgp_peering_address1"] = prompt_ip_line(
        "VPN gateway BGP peering address 1",
        f"{project_name}:vpn_gw_parameters.bgp_peering_address1",
        get_nested("vpn_gw_parameters", "bgp_peering_address1", "169.254.21.10"),
    )
    vpn_gw["bgp_peering_address2"] = prompt_ip_line(
        "VPN gateway BGP peering address 2",
        f"{project_name}:vpn_gw_parameters.bgp_peering_address2",
        get_nested("vpn_gw_parameters", "bgp_peering_address2", "169.254.21.14"),
    )
    config[f"{project_name}:vpn_gw_parameters"] = vpn_gw

    stack_data["config"] = config
    with open(stack_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(stack_data, f, default_flow_style=False, sort_keys=False)
    fix_pulumi_stack_yaml_permissions(stack_file)
    msg("Captured required azure-pa-hub-network values into stack config.", COLOR_GREEN)


# -----------------------------------------------------------------------------
# NSG validation + stack YAML write (Azure Resource Manager rules)
# -----------------------------------------------------------------------------
# Canonicalize protocol/direction/access and address literals so azurerm accepts them; enforce unique
# names/priorities. write_config_value_to_stack_file runs prepare_nsg_rules_for_stack_yaml when the key
# is project:nsg_rules or project:hub_nsg_rules (unless nsg_rules_prevalidated=True).

# AzureRM NSG protocols are case-sensitive (Udp not UDP).
NSG_PROTOCOL_ALLOWED = frozenset({"*", "Tcp", "Udp", "Icmp", "Ah", "Esp"})
NSG_PROTOCOL_LOWER = {"tcp": "Tcp", "udp": "Udp", "icmp": "Icmp", "ah": "Ah", "esp": "Esp"}


def normalize_azure_nsg_protocol(value: str) -> str:
    s = str(value).strip()
    if s == "*":
        return "*"
    if s in NSG_PROTOCOL_ALLOWED:
        return s
    canon = NSG_PROTOCOL_LOWER.get(s.lower())
    if canon is not None:
        return canon
    raise ValueError(
        f"Protocol {value!r} is invalid; use *, Tcp, Udp, Icmp, Ah, or Esp (Azure spelling)."
    )


# Azure NSG address prefixes: literals must be *, a CIDR, or a real service tag (not e.g. "vnet").
NSG_ADDRESS_LITERAL_ALIASES = {
    "vnet": "VirtualNetwork",
    "virtualnetwork": "VirtualNetwork",
    "internet": "Internet",
    "azureloadbalancer": "AzureLoadBalancer",
}
NSG_LITERAL_LOWER_WORD = re.compile(r"^[a-z]+$")


def normalize_nsg_menu_literal(value: str) -> Tuple[str, Optional[str]]:
    """Return (canonical, original_if_aliased)."""
    s = value.strip()
    low = s.lower()
    if low in NSG_ADDRESS_LITERAL_ALIASES:
        return NSG_ADDRESS_LITERAL_ALIASES[low], s
    return s, None


def validate_nsg_menu_literal(value: str, *, rule_name: str, field: str) -> None:
    if value == "*":
        return
    try:
        ipaddress.ip_network(value, strict=False)
        return
    except ValueError:
        pass
    if NSG_LITERAL_LOWER_WORD.fullmatch(value):
        raise ValueError(
            f"NSG rule {rule_name!r}: {field} {value!r} is invalid for Azure. "
            "Use a CIDR, '*', or a service tag (e.g. VirtualNetwork, not vnet). "
            "https://learn.microsoft.com/en-us/azure/virtual-network/service-tags-overview"
        )


def finalize_nsg_menu_literal_prefix(value: str, *, rule_name: str, field: str) -> str:
    """Normalize common YAML mistakes and validate before writing stack config."""
    normalized, original = normalize_nsg_menu_literal(value)
    if original is not None and original != normalized:
        msg(
            f"NSG rule {rule_name!r}: {field} normalized {original!r} → {normalized!r}.",
            COLOR_CYAN,
        )
    validate_nsg_menu_literal(normalized, rule_name=rule_name, field=field)
    return normalized


def normalize_nsg_ref_key_for_project(ref_key: str, project_name: str) -> str:
    """Map menu shortcuts to real Pulumi config keys (spoke has vnet1_cidr, not vnet)."""
    k = ref_key.strip()
    if project_name == "azure-spoke-network" and k.lower() == "vnet":
        msg(
            "Spoke stack config uses vnet1_cidr, not vnet — storing address prefix ref as vnet1_cidr.",
            COLOR_CYAN,
        )
        return "vnet1_cidr"
    return k


NSG_PORT_RANGE_RE = re.compile(r"^\*$|^\d+(-\d+)?$")


def nsg_rules_base_from_config_key(config_key: str) -> str | None:
    """Return 'nsg_rules' or 'hub_nsg_rules' when config_key names that object (after optional project:)."""
    base = config_key.split(":", 1)[-1] if ":" in config_key else config_key
    if base in ("nsg_rules", "hub_nsg_rules"):
        return base
    return None


def normalize_nsg_enum_field(
    value: object,
    *,
    field: str,
    lower_to_canon: dict[str, str],
    allowed_canon: frozenset[str],
    err_hint: str,
) -> str:
    """Map user casing to Azure's fixed strings for direction or access (shared by both)."""
    s = str(value).strip()
    low = s.lower()
    if low in lower_to_canon:
        return lower_to_canon[low]
    if s in allowed_canon:
        return s
    raise ValueError(f"NSG {field} {value!r} is invalid; use {err_hint}.")


def normalize_nsg_direction(value: object) -> str:
    return normalize_nsg_enum_field(
        value,
        field="direction",
        lower_to_canon={"inbound": "Inbound", "outbound": "Outbound"},
        allowed_canon=frozenset({"Inbound", "Outbound"}),
        err_hint="Inbound or Outbound.",
    )


def normalize_nsg_access(value: object) -> str:
    return normalize_nsg_enum_field(
        value,
        field="access",
        lower_to_canon={"allow": "Allow", "deny": "Deny"},
        allowed_canon=frozenset({"Allow", "Deny"}),
        err_hint="Allow or Deny.",
    )


def validate_nsg_port_range(value: object, *, field: str, rule_name: str) -> None:
    s = str(value).strip()
    if not NSG_PORT_RANGE_RE.match(s):
        raise ValueError(
            f"NSG rule {rule_name!r}: {field} {s!r} is invalid; use * or a single port or range (e.g. 443 or 80-443)."
        )


def canonicalize_nsg_literal_prefix_in_rule(
    rule: dict, key: str, rule_name: str, log_aliases: bool
) -> None:
    if key not in rule:
        return
    v = rule[key]
    if v is None:
        return
    s = str(v).strip()
    if not s:
        del rule[key]
        return
    norm, orig = normalize_nsg_menu_literal(s)
    if orig is not None and log_aliases and orig != norm:
        msg(
            f"NSG rule {rule_name!r}: {key.replace('_', ' ')} normalized {orig!r} → {norm!r}.",
            COLOR_CYAN,
        )
    validate_nsg_menu_literal(norm, rule_name=rule_name, field=key.replace("_", " "))
    rule[key] = norm


def canonicalize_nsg_rule_for_azure(rule: dict, *, log_aliases: bool) -> None:
    """Mutate one rule dict: protocol, direction, access, literal address prefixes."""
    rn = str(rule.get("name", "?")).strip() or "?"
    rule["protocol"] = normalize_azure_nsg_protocol(rule.get("protocol", "*"))
    rule["direction"] = normalize_nsg_direction(rule.get("direction", "Inbound"))
    rule["access"] = normalize_nsg_access(rule.get("access", "Allow"))
    canonicalize_nsg_literal_prefix_in_rule(rule, "source_address_prefix", rn, log_aliases)
    canonicalize_nsg_literal_prefix_in_rule(rule, "destination_address_prefix", rn, log_aliases)


def validate_nsg_rules_list_consistency(rules: list) -> None:
    """Structural checks Azure expects (unique names/priorities, xor ref vs literal, ports)."""
    names_seen: dict[str, int] = {}
    priorities_seen: dict[int, str] = {}
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f"nsg_rules[{i}] must be a mapping, not {type(rule).__name__}.")
        n = rule.get("name")
        if n is None or not str(n).strip():
            raise ValueError(f"nsg_rules[{i}] needs a non-empty name.")
        ns = str(n).strip()
        if len(ns) > 80:
            raise ValueError(f"NSG rule {ns!r}: name exceeds Azure's 80-character limit.")
        if ns in names_seen:
            raise ValueError(
                f"Duplicate NSG rule name {ns!r} (entries #{names_seen[ns] + 1} and #{i + 1}). "
                "Azure requires unique names per NSG."
            )
        names_seen[ns] = i
        pr = rule.get("priority")
        try:
            p = int(pr)
        except (TypeError, ValueError) as e:
            raise ValueError(f"NSG rule {ns!r} has invalid priority {pr!r} (must be an integer).") from e
        if p < 100 or p > 4096:
            raise ValueError(f"NSG rule {ns!r}: priority {p} must be between 100 and 4096.")
        if p in priorities_seen:
            raise ValueError(
                f"Duplicate NSG priority {p} on rules {priorities_seen[p]!r} and {ns!r}. "
                "Each rule must have a unique priority."
            )
        priorities_seen[p] = ns
        desc = rule.get("description", "")
        if len(str(desc)) > 140:
            raise ValueError(f"NSG rule {ns!r}: description exceeds 140 characters (Azure limit).")

        def nonempty_ref(k: str) -> bool:
            return k in rule and rule[k] is not None and str(rule[k]).strip() != ""

        sl = nonempty_ref("source_address_prefix")
        sr = nonempty_ref("source_address_prefix_ref")
        if sl and sr:
            raise ValueError(
                f"NSG rule {ns!r}: use only one of source_address_prefix and source_address_prefix_ref."
            )
        if not sl and not sr:
            raise ValueError(
                f"NSG rule {ns!r}: set source_address_prefix or source_address_prefix_ref."
            )
        dl = nonempty_ref("destination_address_prefix")
        dr = nonempty_ref("destination_address_prefix_ref")
        if dl and dr:
            raise ValueError(
                f"NSG rule {ns!r}: use only one of destination_address_prefix and destination_address_prefix_ref."
            )
        if not dl and not dr:
            raise ValueError(
                f"NSG rule {ns!r}: set destination_address_prefix or destination_address_prefix_ref."
            )
        for port_key in ("source_port_range", "destination_port_range"):
            validate_nsg_port_range(
                rule.get(port_key, "*"),
                field=port_key.replace("_", " "),
                rule_name=ns,
            )


def prepare_nsg_rules_for_stack_yaml(rules: list, *, log_aliases: bool = True) -> list:
    """Deep-copy, canonicalize each rule, validate list; returned list is safe to write to stack YAML."""
    out = copy.deepcopy(rules)
    for rule in out:
        canonicalize_nsg_rule_for_azure(rule, log_aliases=log_aliases)
    validate_nsg_rules_list_consistency(out)
    return out


def check_nsg_rules_in_stack_file(stack_file: str, nsg_config_key: str) -> None:
    """Read stack YAML and run NSG validation only (no write). Use to catch issues before pulumi up."""
    data = load_yaml_file(stack_file, required=True)
    config = data.get("config") or {}
    rules = get_stack_config_value(config, nsg_config_key)
    if not isinstance(rules, list):
        msg(f"No NSG rules list under {nsg_config_key!r}.", COLOR_ORANGE)
        return
    try:
        prepare_nsg_rules_for_stack_yaml(rules, log_aliases=False)
    except ValueError as e:
        msg(str(e), COLOR_ORANGE)
        return
    msg("NSG rules pass validation (names, priorities, prefixes, ports, protocol).", COLOR_GREEN)


def write_config_value_to_stack_file(
    stack_file: str,
    config_key: str,
    value: dict | list,
    *,
    nsg_rules_prevalidated: bool = False,
) -> None:
    """Update one namespaced key under config: in Pulumi.<stack>.yaml (dict/list values for complex types)."""
    if not os.path.isfile(stack_file):
        fail(f"Stack file not found: {stack_file}")
    pn = get_project_name()
    if (
        isinstance(value, list)
        and nsg_rules_base_from_config_key(config_key)
        and not nsg_rules_prevalidated
    ):
        try:
            value = prepare_nsg_rules_for_stack_yaml(value, log_aliases=True)
        except ValueError as e:
            fail(str(e))
    data = load_yaml_file(stack_file, required=True)
    config = data.get("config")
    if config is None:
        data["config"] = config = {}
    config[config_key] = value
    with open(stack_file, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    fix_pulumi_stack_yaml_permissions(stack_file)


def prompt_peer_remote_vnet_id(peering_index: int, initial: str) -> str:
    """Require non-empty, non-sample ARM ID for a peering."""
    while True:
        raw = input_line_or_exit(
            f"  Peering #{peering_index + 1} remote_vnet_id (full ARM ID) [{initial}]: "
        )
        val = raw if raw else initial
        if not val:
            msg("remote_vnet_id cannot be empty.", COLOR_ORANGE)
            continue
        if is_placeholder_config_string(val):
            msg("Enter the real remote VNet resource ID (not the sample subscription placeholder).", COLOR_ORANGE)
            continue
        return val


def prompt_azure_spoke_network_extra_config(new_config: dict, project_name: str) -> None:
    """
    Prompt for vnet CIDR, on-prem range, and all peering fields. Removes cloud_network_space
    from config for this project. Mutates new_config. q/quit exits the program.
    """
    vnet_key = f"{project_name}:vnet1_cidr"
    prem_key = f"{project_name}:on_prem_source_ip_range"
    cloud_key = f"{project_name}:cloud_network_space"
    peer_key = f"{project_name}:peerings"

    new_config.pop(cloud_key, None)
    new_config.pop("cloud_network_space", None)

    v_initial = str(new_config.get(vnet_key) or "")
    p_initial = str(new_config.get(prem_key) or "")

    msg("Spoke addressing and connectivity (required):", COLOR_CYAN)
    vnet = prompt_cidr_line("Spoke VNET / subnet CIDR", vnet_key, v_initial)
    on_prem = prompt_cidr_line("On-prem source range (for NSG/routes)", prem_key, p_initial)
    new_config[vnet_key] = vnet
    new_config[prem_key] = on_prem

    peerings = new_config.get(peer_key)
    if not isinstance(peerings, list) or not peerings:
        msg(f"No peerings list in sample under {peer_key}; skipping peering prompts.", COLOR_ORANGE)
        return

    msg("Peerings (name, remote VNet ARM ID, hub/remote CIDR for each entry in the sample):", COLOR_CYAN)
    route_prefix = resolve_route_prefix_from_config(new_config, project_name)
    new_peerings: list = []
    for i, entry in enumerate(peerings):
        if not isinstance(entry, dict):
            new_peerings.append(copy.deepcopy(entry))
            continue
        n0 = substitute_route_template_prefix_in_name(str(entry.get("name") or ""), route_prefix)
        r0 = str(entry.get("remote_vnet_id") or "")
        c0 = str(entry.get("cidr") or "")

        msg(f"Peering #{i + 1}:", COLOR_CYAN)
        name = prompt_line_required("  name", "peerings[].name", n0)
        rv = prompt_peer_remote_vnet_id(i, r0)
        cid = prompt_cidr_line("  cidr (remote network space)", f"peerings[{i}].cidr", c0)
        new_peerings.append({"name": name, "remote_vnet_id": rv, "cidr": cid})

    new_config[peer_key] = new_peerings


def derive_aadds_dns_servers_from_vnet_space(vnet_space: str) -> list[str]:
    """
    Build AADDS DNS server IPs from an AADDS /24 VNET CIDR.
    Uses host offsets .132 and .133 to match current production convention.
    """
    net = ipaddress.ip_network(str(vnet_space).strip(), strict=True)
    if net.version != 4 or net.prefixlen != 24:
        raise ValueError("aadds_vnet_space must be an IPv4 /24 network.")
    return [str(net.network_address + 132), str(net.network_address + 133)]


def prompt_aadds_vnet_space_line(label: str, key_display: str, initial: str) -> str:
    """Prompt for AADDS VNET CIDR and enforce IPv4 /24."""
    while True:
        s = prompt_line_required(label, key_display, initial)
        try:
            n = ipaddress.ip_network(str(s).strip(), strict=True)
            if n.version != 4 or n.prefixlen != 24:
                msg("aadds_vnet_space must be an IPv4 /24 (example: 10.100.101.0/24).", COLOR_ORANGE)
                continue
            return str(n)
        except ValueError:
            msg(f"Not a valid IPv4 /24 CIDR: {s!r}", COLOR_ORANGE)


def azure_domain_services_repo_root_for_menu() -> str:
    """Repo root (directory containing stack_menu.py) for PFX discovery and path hints."""
    return os.path.dirname(os.path.abspath(__file__))


def list_pfx_files_in_domain_services_repo() -> list[str]:
    """Top-level `*.pfx` filenames under the azure-domain-services repo directory."""
    root = azure_domain_services_repo_root_for_menu()
    out: list[str] = []
    try:
        for name in sorted(os.listdir(root)):
            if not str(name).lower().endswith(".pfx"):
                continue
            full = os.path.join(root, name)
            if os.path.isfile(full):
                out.append(name)
    except OSError:
        return []
    return out


def prompt_aadds_pfx_cert_path_for_new_stack(project_name: str, initial: str) -> str:
    """
    When creating a new azure-domain-services stack: pick among repo-root .pfx files by number,
    confirm when only one exists, or enter a custom relative/absolute path.
    """
    pfx_key = f"{project_name}:aadds-pfx-cert-path"
    root = azure_domain_services_repo_root_for_menu()
    candidates = list_pfx_files_in_domain_services_repo()
    if not candidates:
        msg(f"No .pfx files found in {root}. Enter path relative to that directory or an absolute path.", COLOR_ORANGE)
        return prompt_line_required(
            "LDAPS PFX path",
            pfx_key,
            (initial or "azdev.pfx").strip() or "azdev.pfx",
        )
    if len(candidates) == 1:
        only = candidates[0]
        msg(f"Found one PKCS#12 file in the repo directory: {only}", COLOR_CYAN)
        raw = input_line_or_exit(f"Use '{only}' for {pfx_key}? [Y/n]: ").strip().lower()
        if raw in ("", "y", "yes"):
            return only
        return prompt_line_required(
            "LDAPS PFX path (relative to repo directory or absolute)",
            pfx_key,
            only,
        )
    msg(f".pfx files in {root}:", COLOR_CYAN)
    for i, name in enumerate(candidates, start=1):
        msg(f"  {i}) {name}", COLOR_CYAN)
    msg("  0) Enter a different path (not listed above)", COLOR_CYAN)
    while True:
        pick = input_line_or_exit(
            f"Select file for {pfx_key} [1-{len(candidates)} or 0]: "
        ).strip()
        if pick == "0":
            return prompt_line_required(
                "LDAPS PFX path (relative to repo directory or absolute)",
                pfx_key,
                (initial or candidates[0]).strip() or candidates[0],
            )
        if pick.isdigit():
            idx = int(pick)
            if 1 <= idx <= len(candidates):
                return candidates[idx - 1]
        msg(f"Enter a number from 0 to {len(candidates)}.", COLOR_ORANGE)


def prompt_azure_domain_services_stack_config_into(
    new_config: dict,
    project_name: str,
    *,
    is_new_stack_flow: bool = False,
) -> None:
    """
    Guided required values for azure-domain-services create/set flows.
    Loads sample route_tables/peerings/NSGs from config copy, then prompts only
    the project's required core values and derives aadds_dns_servers from /24.
    """
    if project_name != "azure-domain-services":
        return

    msg(
        "For azure-domain-services: core naming/networking, hub stack reference, MS01 VM name, Key Vault, and LDAPS PFX path.",
        COLOR_CYAN,
    )
    msg("Press Enter to keep the value shown in [brackets].", COLOR_CYAN)

    loc_key = "azure-native:location"
    new_config[loc_key] = prompt_line_required(
        "Azure region",
        loc_key,
        str(new_config.get(loc_key) or "westus"),
    )

    def get_top(key: str, default: str = "") -> str:
        return str(new_config.get(f"{project_name}:{key}") or default).strip()

    new_config[f"{project_name}:rg_prefix"] = prompt_line_required(
        "Resource group prefix",
        f"{project_name}:rg_prefix",
        get_top("rg_prefix", "TEST-AADDS"),
    )
    new_config[f"{project_name}:aadds_name"] = prompt_line_required(
        "AADDS domain name",
        f"{project_name}:aadds_name",
        get_top("aadds_name", "az.contoso.edu"),
    )
    vnet_space = prompt_aadds_vnet_space_line(
        "AADDS VNET CIDR (/24 required)",
        f"{project_name}:aadds_vnet_space",
        get_top("aadds_vnet_space", "10.13.0.0/24"),
    )
    new_config[f"{project_name}:aadds_vnet_space"] = vnet_space
    new_config[f"{project_name}:aadds_dns_servers"] = derive_aadds_dns_servers_from_vnet_space(vnet_space)
    msg(
        f"Derived {project_name}:aadds_dns_servers = {new_config[f'{project_name}:aadds_dns_servers']}",
        COLOR_CYAN,
    )

    new_config[f"{project_name}:on_prem_source_ip_range"] = prompt_cidr_line(
        "On-prem source CIDR",
        f"{project_name}:on_prem_source_ip_range",
        get_top("on_prem_source_ip_range", "10.10.0.0/16"),
    )
    hub_ref_key = f"{project_name}:pa_hub_stack"
    new_config[hub_ref_key] = prompt_pa_hub_stack(hub_ref_key, str(new_config.get(hub_ref_key) or ""))

    ms01_obj = new_config.get(f"{project_name}:ms01_vm")
    if not isinstance(ms01_obj, dict):
        ms01_obj = {}
    ms01_obj["vm_name"] = prompt_line_required(
        "Management VM name (ms01_vm.vm_name)",
        f"{project_name}:ms01_vm.vm_name",
        str(ms01_obj.get("vm_name") or "aaddsms01"),
    )
    if not str(ms01_obj.get("admin_username") or "").strip():
        ms01_obj["admin_username"] = "azadmin"
    new_config[f"{project_name}:ms01_vm"] = ms01_obj

    kv_obj = new_config.get(f"{project_name}:key_vault")
    if isinstance(kv_obj, dict):
        kv_obj = copy.deepcopy(kv_obj)
        raw_keys = kv_obj.get("keys")
        if isinstance(raw_keys, list):
            kv_obj["keys"] = [
                x
                for x in raw_keys
                if not (
                    (isinstance(x, dict) and str(x.get("name") or "").strip() == "aadds-pfx-cert-string")
                    or (isinstance(x, str) and str(x).strip() == "aadds-pfx-cert-string")
                )
            ]
    if not isinstance(kv_obj, dict):
        kv_obj = {
            "name": "",
            "keys": [
                {"name": "aadds-pfx-password", "description": "PFX password for AADDS LDAPS certificate"},
                {"name": "aadds-ms01-admin-pw", "description": "Local admin password for MS01 management VM"},
            ],
            "iam_groups": [],
        }
    kv_obj["name"] = prompt_line_required(
        "Key Vault name",
        f"{project_name}:key_vault.name",
        str(kv_obj.get("name") or "test-aadds-kv-replace-me"),
    )
    new_config[f"{project_name}:key_vault"] = kv_obj

    pfx_path_key = f"{project_name}:aadds-pfx-cert-path"
    pfx_initial = str(new_config.get(pfx_path_key) or "azdev.pfx").strip() or "azdev.pfx"
    if is_new_stack_flow:
        new_config[pfx_path_key] = prompt_aadds_pfx_cert_path_for_new_stack(project_name, pfx_initial)
    else:
        new_config[pfx_path_key] = prompt_line_required(
            "LDAPS PFX path (relative to repo directory or absolute)",
            pfx_path_key,
            pfx_initial,
        )


def prompt_pa_hub_stack(stack_key: str, initial: str) -> str:
    """
    Require a non-empty StackReference to azure-pa-hub-network. Reject the committed sample path
    (org/azure-pa-hub-network/...) so creators set a real backend stack name.
    """
    msg(
        "Hub stack reference (required): Pulumi backend name for the azure-pa-hub-network stack "
        "that exports trust_nic_private_ip / untrust_nic_private_ip "
        "(e.g. mycompany/azure-pa-hub-network/dev).",
        COLOR_CYAN,
    )
    while True:
        raw = input_line_or_exit(f"{stack_key} [{initial}]: ")
        val = (raw if raw else initial).strip()
        if not val:
            msg("pa_hub_stack cannot be empty.", COLOR_ORANGE)
            continue
        low = val.lower()
        if low == "org/azure-pa-hub-network/dev" or low.startswith("org/azure-pa-hub-network/"):
            msg(
                "Use your real org/stack path, not the sample org/azure-pa-hub-network/... placeholder.",
                COLOR_ORANGE,
            )
            continue
        if is_placeholder_config_string(val):
            msg("That value still looks like a template placeholder.", COLOR_ORANGE)
            continue
        return val


def prompt_core_infra_stack_for_prod_vms(stack_key: str, initial: str) -> str:
    """
    Require a non-empty StackReference to the core infra stack exporting hub subnet IDs.
    Reject committed sample org/... placeholders.
    """
    msg(
        "Core infrastructure stack reference (required): Pulumi backend name of the core stack "
        "that exports hub1_subnet_id and hub2_subnet_id "
        "(e.g. mycompany/azure-core-infrastructure/prod).",
        COLOR_CYAN,
    )
    while True:
        raw = input_line_or_exit(f"{stack_key} [{initial}]: ")
        val = (raw if raw else initial).strip()
        if not val:
            msg("core_infra_stack cannot be empty.", COLOR_ORANGE)
            continue
        low = val.lower()
        if low == "org/azure-core-infrastructure/prod" or low.startswith("org/azure-core-infrastructure/"):
            msg(
                "Use your real org/stack path, not the sample org/azure-core-infrastructure/... placeholder.",
                COLOR_ORANGE,
            )
            continue
        if is_placeholder_config_string(val):
            msg("That value still looks like a template placeholder.", COLOR_ORANGE)
            continue
        return val


def prompt_network_stack_for_vms(stack_key: str, initial: str) -> str:
    """
    Require a real Pulumi stack reference for network.stack (hub or spoke).
    Reject committed org/... sample paths like spoke/hub samples.
    """
    msg(
        "Referenced network stack (required): Pulumi backend name of the hub or spoke stack "
        "that exports the subnet output (e.g. myorg/azure-spoke-network/dev or "
        "myorg/azure-pa-hub-network/dev).",
        COLOR_CYAN,
    )
    while True:
        raw = input_line_or_exit(f"{stack_key} [{initial}]: ")
        val = (raw if raw else initial).strip()
        if not val:
            msg("network.stack cannot be empty.", COLOR_ORANGE)
            continue
        low = val.lower()
        if low == "org/azure-spoke-network/dev" or low.startswith("org/azure-spoke-network/"):
            msg(
                "Use your real org/stack path, not the sample org/azure-spoke-network/... placeholder.",
                COLOR_ORANGE,
            )
            continue
        if low == "org/azure-pa-hub-network/dev" or low.startswith("org/azure-pa-hub-network/"):
            msg(
                "Use your real org/stack path, not the sample org/azure-pa-hub-network/... placeholder.",
                COLOR_ORANGE,
            )
            continue
        if is_placeholder_config_string(val):
            msg("That value still looks like a template placeholder.", COLOR_ORANGE)
            continue
        return val


def prompt_azure_vms_stack_config_into(config: dict, project_name: str) -> None:
    """
    Guided required values for *-vms stacks: location, rg_prefix, key_vault.name,
    network.stack, network.subnet_id, linux-vms[0] (vm_name, admin_username).

    Does not prompt for bastion_name: Azure Bastion is optional hub config (`bastion` on
    azure-pa-hub-network). Copy this script to other repos when aligning; older copies may
    still prompt for bastion_name until updated.

    Mutates config (top-level Pulumi keys). q/quit exits via input_line_or_exit.
    """
    if not is_vms_stack_project(project_name):
        return

    loc_key = "azure-native:location"
    sample_loc = str(config.get(loc_key) or "westus")

    msg("For *-vms stacks: location, naming, Key Vault, network reference, and VM.", COLOR_CYAN)
    msg("Press Enter to keep the value shown in [brackets].", COLOR_CYAN)

    loc_raw = input_line_or_exit(f"Azure region ({loc_key}) [{sample_loc}]: ")
    config[loc_key] = loc_raw if loc_raw else sample_loc

    def get_top(key: str, default: str = "") -> str:
        return str(config.get(f"{project_name}:{key}") or default).strip()

    def get_nested(parent_key: str, leaf_key: str, default: str = "") -> str:
        obj = config.get(f"{project_name}:{parent_key}")
        if isinstance(obj, dict):
            return str(obj.get(leaf_key) or default).strip()
        return default

    config[f"{project_name}:rg_prefix"] = prompt_line_required(
        "Resource group prefix",
        f"{project_name}:rg_prefix",
        get_top("rg_prefix", "TEST"),
    )

    net_obj = config.get(f"{project_name}:network")
    if not isinstance(net_obj, dict):
        net_obj = {"stack": "", "subnet_id": ""}
    stack_initial = str(net_obj.get("stack") or "org/azure-spoke-network/dev")
    subnet_initial = str(net_obj.get("subnet_id") or "TEST-WESTUS-subnet1-id")
    net_stack = prompt_network_stack_for_vms(
        f"{project_name}:network.stack",
        stack_initial,
    )
    subnet_out = prompt_line_required(
        "Subnet ID output name on referenced stack (e.g. hub2_subnet_id)",
        f"{project_name}:network.subnet_id",
        subnet_initial,
    )
    config[f"{project_name}:network"] = {"stack": net_stack, "subnet_id": subnet_out}

    kv_obj = config.get(f"{project_name}:key_vault")
    if not isinstance(kv_obj, dict):
        kv_obj = copy.deepcopy(build_azure_dev_vms_key_vault())
    else:
        kv_obj = copy.deepcopy(kv_obj)
        if not kv_obj.get("keys"):
            kv_obj["keys"] = copy.deepcopy(build_azure_dev_vms_key_vault()["keys"])
        if "iam_groups" not in kv_obj:
            kv_obj["iam_groups"] = []
    kv_obj["name"] = prompt_line_required(
        "Key Vault name (globally unique)",
        f"{project_name}:key_vault.name",
        str(kv_obj.get("name") or "test-dev-kv-replace-me"),
    )
    config[f"{project_name}:key_vault"] = kv_obj

    vm_lists = build_azure_vms_os_vm_lists(config, project_name)
    linux_vm = vm_lists["linux"][0] if vm_lists.get("linux") else {}
    windows_vm = vm_lists["windows"][0] if vm_lists.get("windows") else {}

    vm_name = prompt_azure_vms_vm_name_line(
        "Linux test VM name",
        f"{project_name}:linux-vms[0].vm_name",
        str(linux_vm.get("vm_name") or AZURE_VMS_LINUX_VM_NAME),
        "linux",
    )
    admin_user = prompt_line_required(
        "Test VM admin username (shared by Linux/Windows defaults)",
        f"{project_name}:linux-vms[0].admin_username",
        str(linux_vm.get("admin_username") or "azadmin"),
    )
    linux_is_allocated = (
        coerce_bool(linux_vm.get("is_allocated")) if "is_allocated" in linux_vm else True
    )
    linux_has_pub_ip = (
        coerce_bool(linux_vm.get("has_pub_ip")) if "has_pub_ip" in linux_vm else False
    )
    shared_admin_secret = str(
        linux_vm.get("admin_password_secret")
        or windows_vm.get("admin_password_secret")
        or AZURE_VMS_ADMIN_PASSWORD_SECRET
    ).strip()
    if not shared_admin_secret:
        shared_admin_secret = AZURE_VMS_ADMIN_PASSWORD_SECRET

    linux_vm["vm_name"] = vm_name
    linux_vm["admin_username"] = admin_user
    linux_vm["admin_password_secret"] = shared_admin_secret
    linux_vm["is_allocated"] = linux_is_allocated
    linux_vm["has_pub_ip"] = linux_has_pub_ip

    windows_vm["vm_name"] = str(
        windows_vm.get("vm_name") or AZURE_VMS_WINDOWS_VM_NAME
    ).strip() or AZURE_VMS_WINDOWS_VM_NAME
    windows_vm["vm_name"] = prompt_azure_vms_vm_name_line(
        "Windows test VM name",
        f"{project_name}:windows-vms[0].vm_name",
        windows_vm["vm_name"],
        "windows",
    )
    windows_vm["admin_username"] = admin_user
    windows_vm["admin_password_secret"] = shared_admin_secret
    windows_vm["is_allocated"] = (
        coerce_bool(windows_vm.get("is_allocated")) if "is_allocated" in windows_vm else False
    )
    windows_vm["has_pub_ip"] = (
        coerce_bool(windows_vm.get("has_pub_ip")) if "has_pub_ip" in windows_vm else False
    )

    config[f"{project_name}:linux-vms"] = vm_lists["linux"]
    config[f"{project_name}:windows-vms"] = vm_lists["windows"]
    config.pop(f"{project_name}:test_vm", None)
    config.pop(f"{project_name}:vms", None)


def prompt_azure_prod_vms_stack_config_into(config: dict, project_name: str) -> None:
    """
    Guided required values for azure-prod-vms stacks.
    Captures provider location, rg_prefix, core_infra_stack, key_vault.name, and VM object names/usernames.
    """
    if project_name != "azure-prod-vms":
        return

    loc_key = "azure-native:location"
    config[loc_key] = prompt_line_required(
        "Azure region",
        loc_key,
        str(config.get(loc_key) or "westus"),
    )

    msg(
        "For azure-prod-vms: location, naming, core infra stack reference, Key Vault, and VM names.",
        COLOR_CYAN,
    )
    msg("Press Enter to keep the value shown in [brackets].", COLOR_CYAN)

    def get_top(key: str, default: str = "") -> str:
        return str(config.get(f"{project_name}:{key}") or default).strip()

    def get_nested(parent_key: str, leaf_key: str, default: str = "") -> str:
        obj = config.get(f"{project_name}:{parent_key}")
        if isinstance(obj, dict):
            return str(obj.get(leaf_key) or default).strip()
        return default

    config[f"{project_name}:rg_prefix"] = prompt_line_required(
        "Resource group prefix",
        f"{project_name}:rg_prefix",
        get_top("rg_prefix", "Enterprise"),
    )

    core_key = f"{project_name}:core_infra_stack"
    config[core_key] = prompt_core_infra_stack_for_prod_vms(
        core_key,
        str(config.get(core_key) or ""),
    )

    kv_obj = config.get(f"{project_name}:key_vault")
    if not isinstance(kv_obj, dict):
        kv_obj = copy.deepcopy(build_azure_prod_vms_key_vault())
    else:
        kv_obj = copy.deepcopy(kv_obj)
        if not kv_obj.get("keys"):
            kv_obj["keys"] = copy.deepcopy(build_azure_prod_vms_key_vault()["keys"])
        if "iam_groups" not in kv_obj:
            kv_obj["iam_groups"] = []
    kv_obj["name"] = prompt_line_required(
        "Key Vault name (globally unique)",
        f"{project_name}:key_vault.name",
        str(kv_obj.get("name") or "enterprise-core-kv-replace-me"),
    )
    config[f"{project_name}:key_vault"] = kv_obj

    dc1_obj = config.get(f"{project_name}:dc1_vm")
    if not isinstance(dc1_obj, dict):
        dc1_obj = {}
    dc1_obj["vm_name"] = prompt_line_required(
        "Domain controller VM name",
        f"{project_name}:dc1_vm.vm_name",
        get_nested("dc1_vm", "vm_name", "citdcaz01"),
    )
    dc1_obj["admin_username"] = prompt_line_required(
        "Domain controller admin username",
        f"{project_name}:dc1_vm.admin_username",
        get_nested("dc1_vm", "admin_username", "azadmin"),
    )
    config[f"{project_name}:dc1_vm"] = dc1_obj

    adconnect_obj = config.get(f"{project_name}:adconnect_vm")
    if not isinstance(adconnect_obj, dict):
        adconnect_obj = {}
    adconnect_obj["vm_name"] = prompt_line_required(
        "AD Connect VM name",
        f"{project_name}:adconnect_vm.vm_name",
        get_nested("adconnect_vm", "vm_name", "citadconnect01"),
    )
    adconnect_obj["admin_username"] = prompt_line_required(
        "AD Connect admin username",
        f"{project_name}:adconnect_vm.admin_username",
        get_nested("adconnect_vm", "admin_username", "azadmin"),
    )
    config[f"{project_name}:adconnect_vm"] = adconnect_obj

    gig_obj = config.get(f"{project_name}:gig_vm")
    if not isinstance(gig_obj, dict):
        gig_obj = {}
    gig_obj["vm_name"] = prompt_line_required(
        "GIG VM name",
        f"{project_name}:gig_vm.vm_name",
        get_nested("gig_vm", "vm_name", "citgigaz"),
    )
    gig_obj["admin_username"] = prompt_line_required(
        "GIG admin username",
        f"{project_name}:gig_vm.admin_username",
        get_nested("gig_vm", "admin_username", "azadmin"),
    )
    config[f"{project_name}:gig_vm"] = gig_obj


def prompt_azure_prod_vms_stack_required_config(stack_file: str, project_name: str) -> None:
    """Write guided azure-prod-vms config into stack YAML (used at start of Set stack variables)."""
    if project_name != "azure-prod-vms":
        return
    stack_data = load_yaml_file(stack_file, required=False)
    if not isinstance(stack_data, dict):
        stack_data = {}
    cfg = stack_data.get("config") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    prompt_azure_prod_vms_stack_config_into(cfg, project_name)
    stack_data["config"] = cfg
    with open(stack_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(stack_data, f, default_flow_style=False, sort_keys=False)
    fix_pulumi_stack_yaml_permissions(stack_file)
    msg("Captured required azure-prod-vms values into stack config.", COLOR_GREEN)


def prompt_ai_services_rg_prefix(initial: str, project_name: str) -> str:
    """
    Prompt for ai-services rg_prefix with constraints so derived storage account names stay valid.
    Storage account name is built as rg_prefix.lower().replace("-", "") + "aidata", so:
    - rg_prefix may include letters, digits, and hyphens
    - spaces/symbols are rejected
    - sanitized length (after removing hyphens) must be <= 18
    """
    key_display = f"{project_name}:rg_prefix"
    while True:
        raw = prompt_line_required("Resource group prefix (rg_prefix)", key_display, initial)
        val = str(raw).strip()
        if not re.fullmatch(r"[A-Za-z0-9-]+", val):
            msg(
                "rg_prefix must use only letters, numbers, and hyphens (no spaces or other symbols).",
                COLOR_ORANGE,
            )
            continue
        stripped = val.replace("-", "")
        if not stripped:
            msg("rg_prefix must include at least one letter or number.", COLOR_ORANGE)
            continue
        if len(stripped) > 18:
            msg(
                "rg_prefix is too long after removing hyphens; keep 18 letters/numbers max so the derived storage account name stays within Azure's 24-char limit.",
                COLOR_ORANGE,
            )
            continue
        return val


def prompt_azure_ai_services_stack_config_into(config: dict, project_name: str) -> None:
    """
    Guided required values for azure-ai-services stacks.
    Captures provider location and the resource group prefix key used by this project (`rg_prefix`).
    """
    if project_name != "azure-ai-services":
        return

    msg("For azure-ai-services: location and resource group prefix.", COLOR_CYAN)
    msg("Press Enter to keep the value shown in [brackets].", COLOR_CYAN)

    config["azure-native:location"] = prompt_line_required(
        "Azure region",
        "azure-native:location",
        str(config.get("azure-native:location") or "eastus2"),
    )

    config[f"{project_name}:rg_prefix"] = prompt_ai_services_rg_prefix(
        str(config.get(f"{project_name}:rg_prefix")),
        project_name,
    )
    # Cleanup legacy key names if present from older stack files.
    config.pop(f"{project_name}:prefix", None)
    config.pop(f"{project_name}:re_prefix", None)


def prompt_azure_ai_services_stack_required_config(stack_file: str, project_name: str) -> None:
    """Write guided azure-ai-services config into stack YAML (used at start of Set stack variables)."""
    if project_name != "azure-ai-services":
        return
    stack_data = load_yaml_file(stack_file, required=False)
    if not isinstance(stack_data, dict):
        stack_data = {}
    cfg = stack_data.get("config") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    prompt_azure_ai_services_stack_config_into(cfg, project_name)
    stack_data["config"] = cfg
    with open(stack_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(stack_data, f, default_flow_style=False, sort_keys=False)
    fix_pulumi_stack_yaml_permissions(stack_file)
    msg("Captured required azure-ai-services values into stack config.", COLOR_GREEN)


def prompt_azure_vms_stack_required_config(stack_file: str, project_name: str) -> None:
    """Write guided *-vms config into the stack YAML (used at start of Set stack variables)."""
    if not is_vms_stack_project(project_name):
        return
    stack_data = load_yaml_file(stack_file, required=False)
    if not isinstance(stack_data, dict):
        stack_data = {}
    cfg = stack_data.get("config") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    prompt_azure_vms_stack_config_into(cfg, project_name)
    stack_data["config"] = cfg
    with open(stack_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(stack_data, f, default_flow_style=False, sort_keys=False)
    fix_pulumi_stack_yaml_permissions(stack_file)
    msg("Captured required *-vms values into stack config.", COLOR_GREEN)


def create_new_stack() -> None:
    """
    Prompt for a stack name, run `pulumi stack init`, then seed `Pulumi.<stack>.yaml` from
    Pulumi.sample.yaml. Subscription and tenant IDs come from `az account show` when available.

    For project `azure-spoke-network`, prompts for region, network_resource_prefix, spoke_prefix
    (derived), required pa_hub_stack (real hub backend name), vnet1_cidr, on_prem_source_ip_range,
    and every field of each peering; omits cloud_network_space. Type q or quit on any prompt to exit.
    For project `azure-domain-services`, prompts for location, rg_prefix, aadds_name,
    aadds_vnet_space (/24 required), on_prem_source_ip_range, pa_hub_stack,
    ms01_vm.vm_name, key_vault.name, and aadds-pfx-cert-path (with .pfx pick-list when multiple
    files exist in the repo); then derives aadds_dns_servers from the /24.
    For project `azure-ai-services`, prompts for location and rg_prefix.
    For project `azure-prod-vms`, prompts for region, rg_prefix, core_infra_stack, key_vault.name,
    dc1_vm, adconnect_vm, and gig_vm.
    For projects whose name ends with `-vms`, prompts for region, rg_prefix, key_vault.name,
    network.stack, network.subnet_id, and linux-vms[0] (not bastion_name; see prompt_azure_vms_stack_config_into).
    Other projects copy the sample and Azure IDs only (except azure-pa-hub-network, which has its own guided flow).
    """
    msg("Enter stack name (e.g. dev or ORG/mystack). Leave blank to use 'dev' (q = quit):", COLOR_CYAN)
    raw = input_line_or_exit("Stack name: ")
    full_name = raw if raw else "dev"
    basename = full_name.split("/")[-1]
    stack_file = f"Pulumi.{basename}.yaml"

    # Check for file conflict before creating the stack. Do not run init if the
    # config file already exists for another stack.
    if os.path.isfile(stack_file):
        stacks = discover_stacks()
        others = [s["full_name"] for s in stacks if s["stack_file"] == stack_file]
        if others:
            msg_stderr(
                f"Config file '{stack_file}' already exists and is used by: {', '.join(others)}.",
                COLOR_RED,
            )
            msg_stderr(
                "Choose a different stack name (e.g. org/stackname) to avoid duplicate config files.",
                COLOR_ORANGE,
            )
            fail("Duplicate stack config file. Use a different stack name.")
        # File exists but no other stack in the backend uses it; we can still init and use it.
        # Fall through to init.

    try:
        subprocess.run(["pulumi", "stack", "init", full_name], check=False)
    except FileNotFoundError:
        fail('Pulumi CLI not found. Install Pulumi or run this from the Pulumi container.')

    current = get_current_stack_full()
    if not current:
        return

    cur_basename = current.split("/")[-1]
    cur_stack_file = f"Pulumi.{cur_basename}.yaml"

    if not os.path.isfile(PULUMI_SAMPLE_FILE):
        msg(
            f"WARNING : {PULUMI_SAMPLE_FILE} not found; add it at the project root. "
            "Creating a minimal stack file with empty config.",
            COLOR_ORANGE,
        )
        if not os.path.isfile(cur_stack_file):
            try:
                with open(cur_stack_file, "w", encoding="utf-8") as f:
                    f.write("config: {}\n")
                fix_pulumi_stack_yaml_permissions(cur_stack_file)
            except OSError as e:
                fail(f"Failed to create local stack file '{cur_stack_file}': {e}")
        msg(f"INFO : Stack '{current}' uses local file '{cur_stack_file}'.", COLOR_CYAN)
        return

    project_name = get_project_name()
    sample_config = load_pulumi_sample_config(required=True)
    new_config = copy.deepcopy(sample_config)

    acct = get_azure_cli_account()
    if acct:
        sid = acct.get("id")
        tid = acct.get("tenantId")
        if sid:
            new_config["azure:subscriptionId"] = sid
        if tid:
            new_config["azure:tenantId"] = tid
        msg("INFO : Applied subscription and tenant from `az account show`.", COLOR_GREEN)
    else:
        msg(
            "WARNING : Azure CLI account not available; subscription/tenant left as in the sample — set them manually.",
            COLOR_ORANGE,
        )

    loc_key = "azure-native:location"
    nrp_key = f"{project_name}:network_resource_prefix"
    spoke_key = f"{project_name}:spoke_prefix"

    if project_name == "azure-spoke-network":
        sample_loc = new_config.get(loc_key) or "westus"
        sample_nrp = new_config.get(nrp_key) or ""

        msg(
            "For azure-spoke-network: region and resource prefix, then hub stack reference, then addressing and peerings.",
            COLOR_CYAN,
        )
        loc_raw = input_line_or_exit(f"Azure region (azure-native:location) [{sample_loc}]: ")
        location = loc_raw if loc_raw else str(sample_loc)

        nrp_raw = input_line_or_exit(
            f"Network resource prefix ({nrp_key}) [{sample_nrp or 'e.g. TEST'}]: "
        )
        nrp = nrp_raw if nrp_raw else sample_nrp
        if not nrp:
            msg("network_resource_prefix cannot be empty.", COLOR_RED)
            return

        new_config[loc_key] = location
        new_config[nrp_key] = nrp
        new_config[spoke_key] = build_spoke_prefix(nrp, location)

        hub_ref_key = f"{project_name}:pa_hub_stack"
        hub_initial = str(new_config.get(hub_ref_key) or "")
        new_config[hub_ref_key] = prompt_pa_hub_stack(hub_ref_key, hub_initial)

        prompt_azure_spoke_network_extra_config(new_config, project_name)
        apply_template_prefixes_to_network_stack_config(new_config, project_name)
    elif project_name == "azure-domain-services":
        prompt_azure_domain_services_stack_config_into(new_config, project_name, is_new_stack_flow=True)
        apply_template_prefixes_to_network_stack_config(new_config, project_name)
    elif project_name == "azure-ai-services":
        prompt_azure_ai_services_stack_config_into(new_config, project_name)
    elif project_name == "azure-prod-vms":
        prompt_azure_prod_vms_stack_config_into(new_config, project_name)
    elif is_vms_stack_project(project_name):
        prompt_azure_vms_stack_config_into(new_config, project_name)
    elif project_name == "azure-pa-hub-network":
        msg(
            "For azure-pa-hub-network: set core prefixes/networking, Key Vault name, Palo Alto names, and gateway BGP values.",
            COLOR_CYAN,
        )
        new_config[f"{project_name}:peerings"] = []
        rg_prefix = prompt_line_required(
            "Resource group prefix",
            f"{project_name}:rg_prefix",
            str(new_config.get(f"{project_name}:rg_prefix") or "ORG"),
        )
        new_config[f"{project_name}:rg_prefix"] = rg_prefix
        new_config[f"{project_name}:bastion"] = build_azure_bastion(
            name=f"{rg_prefix}-hub-bastion",
            is_allocated=False,
        )
        nrp = prompt_line_required(
            "Network resource prefix",
            f"{project_name}:network_resource_prefix",
            str(new_config.get(f"{project_name}:network_resource_prefix") or "ORG-TEST"),
        )
        new_config[f"{project_name}:network_resource_prefix"] = nrp
        new_config[f"{project_name}:vnet"] = prompt_cidr_line(
            "Hub VNET CIDR",
            f"{project_name}:vnet",
            str(new_config.get(f"{project_name}:vnet") or "10.0.0.0/22"),
        )
        new_config[f"{project_name}:on_prem_source_ip_range"] = prompt_cidr_line(
            "On-prem source CIDR",
            f"{project_name}:on_prem_source_ip_range",
            str(new_config.get(f"{project_name}:on_prem_source_ip_range") or "10.10.0.0/16"),
        )

        key_vault_obj = new_config.get(f"{project_name}:key_vault")
        if not isinstance(key_vault_obj, dict):
            key_vault_obj = copy.deepcopy(build_azure_key_vault())
        key_vault_obj["name"] = prompt_line_required(
            "Key Vault name",
            f"{project_name}:key_vault.name",
            str(key_vault_obj.get("name") or "test-hub-kv-replace-me"),
        )
        new_config[f"{project_name}:key_vault"] = key_vault_obj

        palo_obj = new_config.get(f"{project_name}:palo_alto_vm")
        if not isinstance(palo_obj, dict):
            palo_obj = copy.deepcopy(build_azure_palo_alto_vm())
        palo_obj["pub_ip_name"] = prompt_line_required(
            "Palo Alto public IP name",
            f"{project_name}:palo_alto_vm.pub_ip_name",
            str(palo_obj.get("pub_ip_name") or "test-pan-pip"),
        )
        palo_obj["vm_name"] = prompt_line_required(
            "Palo Alto VM name",
            f"{project_name}:palo_alto_vm.vm_name",
            str(palo_obj.get("vm_name") or "test-pan-fw01"),
        )
        new_config[f"{project_name}:palo_alto_vm"] = palo_obj

        local_gw = new_config.get(f"{project_name}:local_gw_parameters")
        if not isinstance(local_gw, dict):
            local_gw = copy.deepcopy(build_azure_local_gw_parameters())
        local_gw["bgp_asn"] = prompt_asn_line(
            "Local gateway BGP ASN",
            f"{project_name}:local_gw_parameters.bgp_asn",
            str(local_gw.get("bgp_asn") or "65001"),
        )
        local_gw["bgp_peering_address"] = prompt_ip_line(
            "Local gateway BGP peering address",
            f"{project_name}:local_gw_parameters.bgp_peering_address",
            str(local_gw.get("bgp_peering_address") or "10.199.0.1"),
        )
        local_gw["connection_ip"] = prompt_ip_line(
            "Local gateway connection IP",
            f"{project_name}:local_gw_parameters.connection_ip",
            str(local_gw.get("connection_ip") or "192.0.2.1"),
        )
        new_config[f"{project_name}:local_gw_parameters"] = local_gw

        vpn_gw = new_config.get(f"{project_name}:vpn_gw_parameters")
        if not isinstance(vpn_gw, dict):
            vpn_gw = copy.deepcopy(build_azure_vpn_gw_parameters())
        vpn_gw["bgp_asn"] = prompt_asn_line(
            "VPN gateway BGP ASN",
            f"{project_name}:vpn_gw_parameters.bgp_asn",
            str(vpn_gw.get("bgp_asn") or "65515"),
        )
        vpn_gw["bgp_peering_address1"] = prompt_ip_line(
            "VPN gateway BGP peering address 1",
            f"{project_name}:vpn_gw_parameters.bgp_peering_address1",
            str(vpn_gw.get("bgp_peering_address1") or "169.254.21.10"),
        )
        vpn_gw["bgp_peering_address2"] = prompt_ip_line(
            "VPN gateway BGP peering address 2",
            f"{project_name}:vpn_gw_parameters.bgp_peering_address2",
            str(vpn_gw.get("bgp_peering_address2") or "169.254.21.14"),
        )
        new_config[f"{project_name}:vpn_gw_parameters"] = vpn_gw
        normalize_hub_peerings_defaults(new_config, project_name)
        apply_template_prefixes_to_network_stack_config(new_config, project_name)

    stack_data: dict = {}
    if os.path.isfile(cur_stack_file):
        loaded = load_yaml_file(cur_stack_file, required=False)
        stack_data = loaded if isinstance(loaded, dict) else {}
    stack_data["config"] = new_config

    try:
        with open(cur_stack_file, "w", encoding="utf-8") as f:
            yaml.safe_dump(stack_data, f, default_flow_style=False, sort_keys=False)
        fix_pulumi_stack_yaml_permissions(cur_stack_file)
    except OSError as e:
        fail(f"Failed to write {cur_stack_file}: {e}")

    msg(
        f"INFO : Wrote stack config from {PULUMI_SAMPLE_FILE} to '{cur_stack_file}'.",
        COLOR_GREEN,
    )
    if project_name == "azure-spoke-network":
        msg(
            f"INFO : {spoke_key} = {new_config.get(spoke_key)!r} (network_resource_prefix + location).",
            COLOR_CYAN,
        )


# -----------------------------------------------------------------------------
# Helpers for current stack
# -----------------------------------------------------------------------------

def get_current_stack_full() -> str | None:
    """
    Get the current Pulumi stack identifier as understood by the CLI.

    Returns values like:
      - "dev"
      - "ORG/dev"
      - "ORG/azure-pa-hub-network/dev"
    or None if no current stack is selected.
    """
    env_stack = os.getenv("PULUMI_STACK")
    if env_stack:
        return env_stack.strip()

    try:
        result = subprocess.run(
            ["pulumi", "stack"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None

    text = result.stdout or ""
    match = re.search(r"Current stack is ([^\s:]+):", text)
    if match:
        return match.group(1).strip()
    return None


# -----------------------------------------------------------------------------
# Interactive menu
# -----------------------------------------------------------------------------

def interactive_menu() -> None:
    """
    Main UI loop until user quits.

    Each iteration: discover stacks, optionally run Key Vault existence checks, print checklist,
    then show one of two menus — (A) no incomplete local stacks: create stack / KV / advanced ops /
    on-prem helper; (B) incomplete stacks: pick active stack, show status line, actions for that stack.
    """
    azure_env = detect_azure_environment()
    kv_done_stacks: set[str] = set()
    kv_exists: dict[str, bool] = {}

    while True:
        # --- Refresh stack list and per-stack completeness (vs Pulumi.sample.yaml) ---
        stacks = discover_stacks()
        summaries = {s["full_name"]: inspect_stack(s) for s in stacks}

        # --- Key Vault preflight (once per stack per session): used for checklist coloring and menu options ---
        # Preflight: for every local stack file with a configured key_vault_name,
        # check whether the Key Vault is deploy-ready (exists + required secrets).
        if azure_env:
            for s in stacks:
                full_name = s["full_name"]
                if not os.path.isfile(s["stack_file"]):
                    continue
                if not summaries.get(full_name, {}).get("kv_required", True):
                    continue
                if not summaries.get(full_name, {}).get("has_kv_name", False):
                    continue
                if full_name in kv_exists:
                    continue
                if not os.path.isfile(CREATE_KEYVAULT_SCRIPT):
                    kv_exists[full_name] = False
                    continue
                try:
                    result = subprocess.run(
                        [
                            sys.executable,
                            "create_keyvault.py",
                            "--check-only",
                            "--stack",
                            full_name,
                        ],
                        cwd=os.getcwd(),
                        capture_output=True,
                        text=True,
                    )
                    kv_exists[full_name] = result.returncode == 0
                except Exception:
                    # If the check fails for any reason (e.g. az auth issues),
                    # treat as "not known" and default to offering creation.
                    kv_exists[full_name] = False

        print_stack_checklist(stacks=stacks, summaries=summaries, kv_exists=kv_exists, azure_env=azure_env)

        incomplete_stacks = [
            s for s in stacks if summaries[s["full_name"]]["status"] == "incomplete"
        ]

        current_stack_full = get_current_stack_full()
        # If there are any incomplete stacks, do not automatically prefer Pulumi's
        # currently-selected stack when entering Menu B. Pulumi's "current stack"
        # might be a different (complete) stack, which would hide the actions
        # needed to fill missing config for the incomplete ones.
        if incomplete_stacks:
            current_stack_full = None
        current_project = get_project_name()

        # ========== Menu A: everyone complete (or no stacks) — global actions + stack picker for some ops ==========
        # Case 1: no stacks or no incomplete stacks -> create / on-prem (only if a stack has cloud_network_space key) / quit.
        if not stacks or not incomplete_stacks:
            local_stacks = [s for s in stacks if os.path.isfile(s["stack_file"])]
            has_onprem_option = (not is_vms_stack_project(current_project)) and any(
                stack_has_cloud_network_space_key(s["stack_file"]) for s in local_stacks
            )

            # Find a complete stack (local file present) so we can offer peering/route updates.
            complete_local_stacks = [
                s
                for s in stacks
                if summaries.get(s["full_name"], {}).get("status") == "complete"
                and os.path.isfile(s["stack_file"])
            ]

            actions: list[tuple[str, callable]] = []
            actions.append(("Create new stack", create_new_stack))
            actions.append(("Backup stack", run_backup_stack))
            if not is_create_backup_only_project(current_project):
                if current_project == "azure-pa-hub-network":
                    actions.append(("Add/Remove a Bastion Host", run_bastion_host_menu))
                if current_project == "azure-vms":
                    actions.append(
                        ("Allocate Linux test VM", lambda: run_set_azure_vms_test_vm_flags("linux"))
                    )
                    actions.append(
                        ("Allocate Windows test VM", lambda: run_set_azure_vms_test_vm_flags("windows"))
                    )
                if has_onprem_option:
                    actions.append(("Check for next available on-prem connecting network space", run_check_next_onprem_network))

            # Eligible stacks for advanced actions:
            # - config variables are complete (inspect_stack status == "complete")
            # - Key Vault is deploy-ready (kv_exists preflight)
            eligible_adv_stacks = [
                s
                for s in complete_local_stacks
                if (
                    not summaries.get(s["full_name"], {}).get("kv_required", True)
                    or (
                        summaries.get(s["full_name"], {}).get("has_kv_name", False)
                        and kv_exists.get(s["full_name"], False)
                    )
                )
            ]

            # Eligible stacks for creating a Key Vault:
            # - stack vars complete (Menu A only)
            # - has key_vault_name configured
            # - preflight says the vault is not deploy-ready yet
            eligible_kv_create_stacks = eligible_stacks_for_keyvault_create(
                stacks,
                summaries,
                kv_exists,
                kv_done_stacks,
                require_complete_config=True,
            )

            if eligible_adv_stacks and not is_create_backup_only_project(current_project):
                if show_peering_and_routes_menu():
                    actions.append(
                        (
                            "Add peering (and routes)",
                            lambda: add_peering_and_routes_to_stack(
                                pick_stack_interactive(
                                    eligible_adv_stacks, "Select stack to add peering/routes:"
                                )
                            ),
                        )
                    )
                if show_ldap_connection_menu(current_project):
                    actions.append(
                        (
                            "Add LDAP connection (and NSG rule)",
                            lambda: add_domain_ldap_connection_to_stack(
                                pick_stack_interactive(
                                    eligible_adv_stacks, "Select stack to add LDAP connection:"
                                )
                            ),
                        )
                    )
                if show_nsg_rule_menu():
                    nsg_menu_label = get_nsg_add_menu_label()
                    actions.append(
                        (
                            nsg_menu_label,
                            lambda label=nsg_menu_label: add_hub_nsg_rule_to_stack(
                                pick_stack_interactive(
                                    eligible_adv_stacks, stack_pick_prompt_for_nsg_action(label)
                                )
                            ),
                        )
                    )
                if show_add_route_table_rule_menu():
                    actions.append(
                        (
                            "Add route table route",
                            lambda: run_route_table_rule_menu_for_stack(
                                pick_stack_interactive(
                                    eligible_adv_stacks,
                                    "Select stack to add a route table route:",
                                )
                            ),
                        )
                    )

            if eligible_kv_create_stacks and azure_env and not is_create_backup_only_project(current_project):
                # Insert right after "Create new stack" for visibility.
                def create_kv_for_selected_stack():
                    chosen = pick_stack_interactive(
                        eligible_kv_create_stacks,
                        "Select stack to create Azure Key Vault:",
                    )
                    create_az_kv(chosen["full_name"])
                    kv_done_stacks.add(chosen["full_name"])
                    kv_exists[chosen["full_name"]] = True

                actions.insert(1, ("Create an Azure Key Vault", create_kv_for_selected_stack))

            msg("Menu:", COLOR_CYAN)
            for idx, (label, _) in enumerate(actions, start=1):
                msg(f"  {idx}) {label}", COLOR_CYAN)
            msg("  q) quit", COLOR_CYAN)
            msg("")

            try:
                choice = input_line_or_exit(f"Select an option [1-{len(actions)}]: ").strip().lower()
            except EOFError:
                msg_stderr("Input closed; exiting.", COLOR_ORANGE)
                break

            if quit_input_detected(choice):
                # Quit from "no incomplete stacks" menu: show pulumi commands only if current stack is complete.
                if current_stack_full and summaries.get(current_stack_full, {}).get("status") == "complete":
                    msg(
                        "INFO : You can run: pulumi preview, pulumi up, pulumi stack output",
                        COLOR_CYAN,
                    )
                break

            if not choice.isdigit():
                msg("Invalid selection. Choose a number from the list or q to quit.", COLOR_ORANGE)
                continue

            idx = int(choice)
            if idx < 1 or idx > len(actions):
                msg("Invalid selection. Choose a number from the list or q to quit.", COLOR_ORANGE)
                continue

            label, func = actions[idx - 1]
            func()
            continue

        # ========== Menu B: at least one incomplete stack — focus one "active" stack ==========
        # There is at least one incomplete stack. Pick an active one.
        # If the current stack is already complete, prefer it so we can offer
        # peering/route and NSG rule update actions.
        active = None
        if current_stack_full and summaries.get(current_stack_full, {}).get("status") == "complete":
            active = next((s for s in stacks if s["full_name"] == current_stack_full), None)

        if active is None:
            if len(incomplete_stacks) == 1:
                active = incomplete_stacks[0]
            else:
                # Two or more incomplete stacks: let user choose which to work with.
                msg("Select which stack to work with:", COLOR_CYAN)
                for i, s in enumerate(incomplete_stacks, start=1):
                    msg(f"  {i}) {s['full_name']}", COLOR_CYAN)
                msg("  q) quit", COLOR_CYAN)
                msg("")
                try:
                    choice = input_line_or_exit(
                        f"Select stack [1-{len(incomplete_stacks)}]: "
                    ).strip().lower()
                except EOFError:
                    msg_stderr("Input closed; exiting.", COLOR_ORANGE)
                    break
                if quit_input_detected(choice):
                    break
                if not choice.isdigit():
                    msg("Invalid selection. Choose a number from the list or q to quit.", COLOR_ORANGE)
                    continue
                idx = int(choice)
                if idx < 1 or idx > len(incomplete_stacks):
                    msg("Invalid selection. Choose a number from the list or q to quit.", COLOR_ORANGE)
                    continue
                active = incomplete_stacks[idx - 1]

        active_name = active["full_name"]
        active_summary = summaries[active_name]
        has_kv_name = active_summary.get("has_kv_name", False)
        kv_required = active_summary.get("kv_required", True)
        missing_required = get_missing_required_config(active["stack_file"])
        stack_variables_done = not missing_required
        kv_done = active_name in kv_done_stacks
        kv_already_exists = kv_exists.get(active_name, False)

        msg(f"Active stack: {active_name}", COLOR_CYAN)
        if stack_variables_done:
            msg("  - Stack variables: SET", COLOR_GREEN)
        else:
            msg("  - Stack variables: INCOMPLETE", COLOR_ORANGE)
        if not kv_required:
            msg("  - Key Vault variable: N/A (not required for this project)", COLOR_CYAN)
        elif has_kv_name:
            msg("  - Key Vault variable: SET", COLOR_GREEN)
        else:
            msg("  - Key Vault variable: NOT SET", COLOR_ORANGE)

        if not kv_required:
            msg("  - Azure Key Vault: N/A (not required for this project)", COLOR_CYAN)
        elif azure_env and has_kv_name:
            if kv_done:
                msg("  - Azure Key Vault: CREATED (this session)", COLOR_GREEN)
            elif kv_already_exists:
                msg("  - Azure Key Vault: EXISTS", COLOR_GREEN)
            else:
                msg("  - Azure Key Vault: NOT READY", COLOR_ORANGE)
        elif not azure_env:
            msg("  - Azure Key Vault: N/A (no Azure provider detected)", COLOR_CYAN)
        else:
            msg("  - Azure Key Vault: BLOCKED (key_vault.name missing)", COLOR_ORANGE)
        if current_project == "azure-vms":
            vm_status = get_azure_vms_test_vm_status(active["stack_file"], current_project)
            if vm_status is None:
                msg("  - Linux test VM: UNKNOWN (no local stack VM config)", COLOR_ORANGE)
                msg("  - Windows test VM: UNKNOWN (no local stack VM config)", COLOR_ORANGE)
            else:
                linux = vm_status["linux"]
                windows = vm_status["windows"]
                linux_alloc = "ALLOCATED" if coerce_bool(linux.get("is_allocated")) else "DEALLOCATED"
                windows_alloc = "ALLOCATED" if coerce_bool(windows.get("is_allocated")) else "DEALLOCATED"
                linux_pub = "ON" if coerce_bool(linux.get("has_pub_ip")) else "OFF"
                windows_pub = "ON" if coerce_bool(windows.get("has_pub_ip")) else "OFF"
                msg(
                    f"  - Linux test VM: {linux_alloc} (has_pub_ip={linux_pub})",
                    COLOR_GREEN if linux_alloc == "ALLOCATED" else COLOR_CYAN,
                )
                msg(
                    f"  - Windows test VM: {windows_alloc} (has_pub_ip={windows_pub})",
                    COLOR_GREEN if windows_alloc == "ALLOCATED" else COLOR_CYAN,
                )

        # Actions depend on whether the active stack is actually complete (e.g. current stack complete but others not).
        actions: list[tuple[str, callable]] = []
        # When "Create an Azure Key Vault" runs for a non-active stack, record it for session bookkeeping.
        menu_b_kv_create_target: list[str | None] = [None]

        # Complete active stack: peerings/routes/NSG edit the YAML directly (no pulumi config set for whole objects).
        if active_summary.get("status") == "complete" and not is_create_backup_only_project(current_project):
            # These actions modify nested objects (peerings + route_tables + NSG rules)
            # in the selected stack YAML (*-vms projects omit all three via profile flags).
            if show_peering_and_routes_menu():
                actions.append(
                    (
                        "Add peering (and routes)",
                        lambda ast=active: add_peering_and_routes_to_stack(ast),
                    )
                )
            if show_ldap_connection_menu(current_project):
                actions.append(
                    (
                        "Add LDAP connection (and NSG rule)",
                        lambda ast=active: add_domain_ldap_connection_to_stack(ast),
                    )
                )
            if show_nsg_rule_menu():
                nsg_menu_label_b = get_nsg_add_menu_label()
                actions.append(
                    (
                        nsg_menu_label_b,
                        lambda ast=active: add_hub_nsg_rule_to_stack(ast),
                    )
                )
            if show_add_route_table_rule_menu():
                actions.append(
                    (
                        "Add route table route",
                        lambda ast=active: run_route_table_rule_menu_for_stack(ast),
                    )
                )

        # Option to create Azure Key Vault: any local stack with key_vault.name and a not-ready vault
        # (not only the active incomplete stack — lets you pick e.g. dev while prod is "active").
        eligible_kv_menu_b = eligible_stacks_for_keyvault_create(
            stacks,
            summaries,
            kv_exists,
            kv_done_stacks,
            require_complete_config=False,
        )
        if (
            keyvault_required_for_project(current_project)
            and azure_env
            and eligible_kv_menu_b
            and not is_create_backup_only_project(current_project)
        ):

            def run_menu_b_create_keyvault():
                chosen = pick_stack_interactive(
                    eligible_kv_menu_b,
                    "Select stack to create Azure Key Vault:",
                )
                menu_b_kv_create_target[0] = chosen["full_name"]
                create_az_kv(chosen["full_name"])

            actions.append(("Create an Azure Key Vault", run_menu_b_create_keyvault))

        # Set stack variables: seed from Pulumi.sample.yaml, then set any missing required (one at a time).
        if (missing_required or (kv_required and not has_kv_name)) and not is_create_backup_only_project(current_project):
            actions.append(
                (
                    "Set stack variables",
                    lambda an=active_name, sf=active["stack_file"]: (
                        seed_from_pulumi_sample(an),
                        run_set_required_variables(an, sf),
                    ),
                )
            )

        # Always allow creating a new stack.
        actions.append(("Create new stack", create_new_stack))
        actions.append(("Backup stack", run_backup_stack))
        if current_project == "azure-pa-hub-network" and not is_create_backup_only_project(current_project):
            actions.append(
                (
                    "Add/Remove a Bastion Host",
                    lambda ast=active: run_bastion_host_menu(ast),
                )
            )
        if current_project == "azure-vms" and not is_create_backup_only_project(current_project):
            actions.append(
                (
                    "Allocate Linux test VM",
                    lambda ast=active: run_set_azure_vms_test_vm_flags("linux", ast),
                )
            )
            actions.append(
                (
                    "Allocate Windows test VM",
                    lambda ast=active: run_set_azure_vms_test_vm_flags("windows", ast),
                )
            )

        # Check next on-prem network (network repos only; not *-vms).
        local_stacks = [s for s in stacks if os.path.isfile(s["stack_file"])]
        if (not is_vms_stack_project(current_project)) and (not is_create_backup_only_project(current_project)) and any(
            stack_has_cloud_network_space_key(s["stack_file"]) for s in local_stacks
        ):
            actions.append(("Check for next available on-prem connecting network space", run_check_next_onprem_network))

        msg("Menu:", COLOR_CYAN)
        for idx, (label, _) in enumerate(actions, start=1):
            msg(f"  {idx}) {label}", COLOR_CYAN)
        msg("  q) quit", COLOR_CYAN)
        msg("")

        try:
            choice = input_line_or_exit(f"Select an option [1-{len(actions)}]: ").strip().lower()
        except EOFError:
            msg_stderr("Input closed; exiting.", COLOR_ORANGE)
            break

        if quit_input_detected(choice):
            # Only suggest pulumi preview/up when the active stack is complete (all required vars set).
            if active_summary.get("status") == "complete":
                stack_file = active["stack_file"]
                msg(
                    f"INFO : Stack configuration file '{stack_file}' is present on this machine.",
                    COLOR_CYAN,
                )
                msg("You can run:", COLOR_CYAN)
                msg("  pulumi preview", COLOR_CYAN)
                msg("  pulumi up", COLOR_CYAN)
                msg("  pulumi stack output", COLOR_CYAN)
            break

        if not choice.isdigit():
            msg("Invalid selection. Choose a number from the list or q to quit.", COLOR_ORANGE)
            continue

        idx = int(choice)
        if idx < 1 or idx > len(actions):
            msg("Invalid selection. Choose a number from the list or q to quit.", COLOR_ORANGE)
            continue

        label, func = actions[idx - 1]

        # Execute the selected action.
        func()

        # If Key Vault was created from Menu B, record session state for the stack that was chosen
        # (may differ from the active stack). Do not gate on active-stack kv_done — that would skip
        # updates when the vault was created for a different stack than the one currently "active".
        if label.startswith("Create an Azure Key Vault"):
            target = menu_b_kv_create_target[0] or active_name
            menu_b_kv_create_target[0] = None
            if target:
                kv_done_stacks.add(target)
                kv_exists[target] = True

# -----------------------------------------------------------------------------
# Main entry point
# -----------------------------------------------------------------------------

def main() -> None:
    """Script entry: start the interactive menu (no CLI args)."""
    interactive_menu()


if __name__ == "__main__":
    main()
