"""An Azure RM Python Pulumi program (Azure Entra Domain Services)."""
# Developed by Andrew Tamagni.
# Domain Services network, NSG rules, route table routes, and peerings are driven
# from Pulumi.<stack>.yaml, with LDAPS PFX read from disk (aadds-pfx-cert-path) and secrets from Key Vault.

import os
import base64
import pulumi
import ipaddress
from pulumi import StackReference
import pulumi_azure as azure_classic
from pulumi_azure_native import resources
import pulumi_azure_native as azure_native
from azure.identity import AzureCliCredential
from azure.keyvault.secrets import SecretClient

pulumi.log.info("Deploying Resources")

######################## Helper Functions ########################
# Internal helper functions to build NSG rules and Route Tables from stack config.
def build_nsg_rules(rules_list: list[dict], cfg: pulumi.Config) -> list:
    """Convert NSG rule config entries into NSG security rule arguments.
    Keeps NSG rule definitions declarative in `Pulumi.<stack>.yaml`.
    """
    out = []
    for rule in rules_list:
        source = resolve_nsg_address(rule, "source_address_prefix", cfg)
        dest = resolve_nsg_address(rule, "destination_address_prefix", cfg)
        out.append(
            azure_classic.network.NetworkSecurityGroupSecurityRuleArgs(
                name=rule["name"],
                protocol=rule.get("protocol", "*"),
                source_port_range=rule.get("source_port_range", "*"),
                destination_port_range=rule.get("destination_port_range", "*"),
                source_address_prefix=source,
                destination_address_prefix=dest,
                access=rule["access"],
                priority=rule["priority"],
                direction=rule["direction"],
            )
        )
    return out


def build_routes(
    route_list: list[dict],
    cfg: pulumi.Config,
    trust_private_ip,
    address_refs: dict | None = None,
):
    """Build `RouteArgs` objects from stack configuration.
    Handles:
    - Route destination prefixes via `resolve_address_prefix`, which allows
      literal CIDRs or dotted config-path refs.
    - Next-hop IPs via `next_hop_ip_ref` (`trust_priv_ip`) so routes follow the
      current trust private IP, while still allowing explicit IP literals.
    """
    out = []
    for route in route_list:
        kwargs = {
            "name": route["name"],
            "address_prefix": resolve_address_prefix(route, cfg, address_refs),
            "next_hop_type": route["next_hop_type"],
        }
        if route.get("next_hop_ip_ref") == "trust_priv_ip":
            kwargs["next_hop_ip_address"] = trust_private_ip
        elif "next_hop_ip_address" in route:
            kwargs["next_hop_ip_address"] = route["next_hop_ip_address"]
        out.append(azure_native.network.RouteArgs(**kwargs))
    return out


def get_kv_secret(secret_name: str) -> str:
    """Fetch a non-empty secret value from the configured Key Vault.
    Raises a ValueError when the secret is missing or has an empty value so that
    failures are caught early during `pulumi preview` or `pulumi up`.
    """
    secret = kv_client.get_secret(secret_name)
    if not secret or not secret.value:
        raise ValueError(f"Key Vault secret '{secret_name}' not found or empty in {key_vault_name}")
    return secret.value


def resolve_address_prefix(route_def: dict, cfg: pulumi.Config, address_refs: dict | None = None):
    """Resolve a route's address prefix from a literal or config path ref."""
    if "address_prefix" in route_def:
        return route_def["address_prefix"]
    ref = route_def["address_prefix_ref"]
    if address_refs and ref in address_refs:
        return address_refs[ref]
    return resolve_config_path(cfg, ref)


def resolve_config_path(cfg: pulumi.Config, path: str):
    """Resolve a dotted config path from Pulumi stack YAML."""
    segments = str(path).split(".")
    key = segments[0]
    if len(segments) == 1:
        return cfg.require(key)
    obj = cfg.require_object(key)
    for segment in segments[1:]:
        if segment.isdigit():
            obj = obj[int(segment)]
        else:
            obj = obj[segment]
    return obj


def resolve_nsg_address(rule: dict, prefix_key: str, cfg: pulumi.Config):
    """Resolve source or destination address for an NSG rule.
    If `<prefix_key>_ref` is present (e.g. `source_address_prefix_ref`), the
    value is treated as a dotted stack-config path.
    Otherwise the literal `<prefix_key>` value is returned.
    """
    ref_key = prefix_key + "_ref"
    if ref_key in rule:
        return resolve_config_path(cfg, rule[ref_key])
    return rule[prefix_key]


######################## Stack Configuration ########################
# Grab variables from Pulumi.<stack>.yaml.

cfg                     = pulumi.Config()
rg_prefix               = cfg.require("rg_prefix")
aadds_name              = cfg.require("aadds_name")
aadds_vnet_space        = cfg.require("aadds_vnet_space")
aadds_dns_servers       = cfg.require_object("aadds_dns_servers")
on_prem_source_ip_range = cfg.require("on_prem_source_ip_range")
pa_hub_stack            = cfg.require("pa_hub_stack")
hub_stack               = StackReference(pa_hub_stack)
hub_vnet_resource_id    = hub_stack.get_output("hub_vnet_resource_id")
hub_vnet_cidr           = hub_stack.get_output("hub_vnet_cidr")
trust_nic_private_ip    = hub_stack.get_output("trust_nic_private_ip")
ms01_cfg                = cfg.require_object("ms01_vm")
ms01_vm_name            = ms01_cfg["vm_name"]
ms01_admin_username     = ms01_cfg["admin_username"]
ms01_admin_pw_secret    = ms01_cfg.get("admin_password_secret", "aadds-ms01-admin-pw")
key_vault_cfg           = cfg.require_object("key_vault")
key_vault_name          = key_vault_cfg["name"]
vault_url               = f"https://{key_vault_name}.vault.azure.net/"
kv_client               = SecretClient(vault_url=vault_url, credential=AzureCliCredential())
pfx_pw                  = get_kv_secret("aadds-pfx-password")

# LDAPS: binary PFX on disk (stack config path, relative to this repo or absolute); password from Key Vault.
pfx_rel = cfg.require("aadds-pfx-cert-path").strip()
program_dir = os.path.dirname(os.path.abspath(__file__))
pfx_path = (
    pfx_rel if os.path.isabs(pfx_rel) else os.path.normpath(os.path.join(program_dir, pfx_rel))
)
if not os.path.isfile(pfx_path):
    raise ValueError(
        f"LDAPS PFX not found at {pfx_path!r} (stack key aadds-pfx-cert-path={pfx_rel!r}). "
        "Use a path relative to the Pulumi program directory or an absolute path."
    )
with open(pfx_path, "rb") as pfx_f:
    pfx_bytes = pfx_f.read()
if not pfx_bytes:
    raise ValueError(f"LDAPS PFX file is empty: {pfx_path}")
try:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.serialization import pkcs12
except ImportError as e:
    raise RuntimeError(
        "Install the 'cryptography' package (pip install -r requirements.txt) to verify the LDAPS PFX."
    ) from e
try:
    pkcs12.load_key_and_certificates(pfx_bytes, pfx_pw.encode("utf-8"), default_backend())
except ValueError as e:
    raise ValueError(
        "Could not open the LDAPS PFX with the password from Key Vault secret 'aadds-pfx-password'. "
        "Check the file and secret."
    ) from e
pfx_cert_string = base64.b64encode(pfx_bytes).decode("ascii")

ms01_admin_pw           = get_kv_secret(ms01_admin_pw_secret)
aadds_ms_nsg_rules_cfg  = cfg.require_object("aadds_ms_nsg_rules")
aadds_nsg_rules_cfg     = cfg.require_object("aadds_nsg_rules")
route_tables_cfg        = cfg.require_object("route_tables")
peerings_cfg            = cfg.require_object("peerings")

######################## Calculate Subnets ########################
# Generate VNET and subnet address spaces from a /24 CIDR for AADDS.
# /25 subnets within VNET address space
subnets_25   = list(
    ipaddress.ip_network(aadds_vnet_space).subnets(new_prefix=25)
)
aadds_ms_vnet = str(subnets_25[0])  # First /25
aadds_vnet    = str(subnets_25[1])  # Second /25

# /26 subnets within VNET address space
subnets_26           = list(
    ipaddress.ip_network(aadds_vnet_space).subnets(new_prefix=26)
)
aadds_ms_subnet1_space = str(subnets_26[0])  # First /26
aadds_ms_subnet2_space = str(subnets_26[1])  # Second /26
aadds_subnet1_space    = str(subnets_26[2])  # Third /26
aadds_subnet2_space    = str(subnets_26[3])  # Fourth /26

######################## First Group Of Resources ########################
# Create resource groups, virtual networks, subnets, and NSGs.
#
# MS = management server: -MS-Networking / -MS-VNET / -MS-NSG hold the admin VM footprint.
# -MS-VNET peers to the hub; the AADDS VNET (-VNET) peers only to -MS-VNET so RSAT management
# stays off the DC subnet while admins still reach the managed domain after joining the VM.

aadds_resource_group = resources.ResourceGroup(str(rg_prefix) + "-Infrastructure",
    resource_group_name=str(rg_prefix) + "-Infrastructure",
)

aadds_ms_networking_resource_group = resources.ResourceGroup(str(rg_prefix) + "-MS-Networking",
    resource_group_name=str(rg_prefix) + "-MS-Networking",
)

aadds_networking_resource_group = resources.ResourceGroup(str(rg_prefix) + "-Networking",
    resource_group_name=str(rg_prefix) + "-Networking",
)

aadds_ms_vm_resource_group = resources.ResourceGroup(str(rg_prefix) + "-" + str(ms01_vm_name) + "-VM",
    resource_group_name=str(rg_prefix) + "-" + str(ms01_vm_name) + "-VM",
)

# AADDS MS VNET and Subnets
aadds_ms_virtual_network = azure_classic.network.VirtualNetwork(str(rg_prefix) + "-MS-VNET",
    opts=pulumi.ResourceOptions(depends_on=[aadds_ms_networking_resource_group]),
    name=str(rg_prefix) + "-MS-VNET",
    location=aadds_ms_networking_resource_group.location,
    resource_group_name=aadds_ms_networking_resource_group.name,
    address_spaces=[aadds_ms_vnet],
    dns_servers=aadds_dns_servers,
)

aadds_ms_subnet1 = azure_classic.network.Subnet(str(rg_prefix) + "-MS-VNET-1",
    opts=pulumi.ResourceOptions(depends_on=[aadds_ms_virtual_network]),
    name=str(rg_prefix) + "-MS-VNET-1",
    resource_group_name=aadds_ms_networking_resource_group.name,
    virtual_network_name=aadds_ms_virtual_network.name,
    address_prefixes=[aadds_ms_subnet1_space],
)

aadds_ms_subnet2 = azure_classic.network.Subnet(str(rg_prefix) + "-MS-VNET-2",
    opts=pulumi.ResourceOptions(depends_on=[aadds_ms_virtual_network]),
    name=str(rg_prefix) + "-MS-VNET-2",
    resource_group_name=aadds_ms_networking_resource_group.name,
    virtual_network_name=aadds_ms_virtual_network.name,
    address_prefixes=[aadds_ms_subnet2_space],
)

# AADDS DC VNET and Subnets
aadds_virtual_network = azure_classic.network.VirtualNetwork(str(rg_prefix) + "-VNET",
    opts=pulumi.ResourceOptions(depends_on=[aadds_networking_resource_group]),
    name=str(rg_prefix) + "-VNET",
    location=aadds_networking_resource_group.location,
    resource_group_name=aadds_networking_resource_group.name,
    address_spaces=[aadds_vnet],
    dns_servers=aadds_dns_servers,
)

aadds_subnet1 = azure_classic.network.Subnet(str(rg_prefix) + "-VNET-1",
    opts=pulumi.ResourceOptions(depends_on=[aadds_virtual_network]),
    name=str(rg_prefix) + "-VNET-1",
    resource_group_name=aadds_networking_resource_group.name,
    virtual_network_name=aadds_virtual_network.name,
    address_prefixes=[aadds_subnet1_space],
)

aadds_subnet2 = azure_classic.network.Subnet(str(rg_prefix) + "-VNET-2",
    opts=pulumi.ResourceOptions(depends_on=[aadds_virtual_network]),
    name=str(rg_prefix) + "-VNET-2",
    resource_group_name=aadds_networking_resource_group.name,
    virtual_network_name=aadds_virtual_network.name,
    address_prefixes=[aadds_subnet2_space],
)

# AADDS MS VNET Network Security Group
aadds_ms_network_security_group = azure_classic.network.NetworkSecurityGroup(str(rg_prefix) + "-MS-NSG",
opts=pulumi.ResourceOptions(depends_on=[aadds_ms_networking_resource_group]),
name=str(rg_prefix) + "-MS-NSG",
    location=aadds_ms_networking_resource_group.location,
    resource_group_name=aadds_ms_networking_resource_group.name,
    security_rules=build_nsg_rules(aadds_ms_nsg_rules_cfg, cfg),
)

aadds_ms_network_security_group_association = azure_classic.network.SubnetNetworkSecurityGroupAssociation(str(rg_prefix) + "-MS-NSG-Association",
    opts=pulumi.ResourceOptions(depends_on=[aadds_ms_network_security_group,aadds_ms_subnet1]),
    subnet_id=aadds_ms_subnet1.id,
    network_security_group_id=aadds_ms_network_security_group.id,
)

# AADDS DC VNET Network Security Group
aadds_network_security_group = azure_classic.network.NetworkSecurityGroup(str(rg_prefix) + "-NSG",
opts=pulumi.ResourceOptions(depends_on=[aadds_networking_resource_group]),
name=str(rg_prefix) + "-NSG",
    location=aadds_networking_resource_group.location,
    resource_group_name=aadds_networking_resource_group.name,
    security_rules=build_nsg_rules(aadds_nsg_rules_cfg, cfg),
)

aadds_network_security_group_association = azure_classic.network.SubnetNetworkSecurityGroupAssociation(str(rg_prefix) + "-NSG-Association",
    opts=pulumi.ResourceOptions(depends_on=[aadds_network_security_group,aadds_subnet1]),
    network_security_group_id=aadds_network_security_group.id,
    subnet_id=aadds_subnet1.id,
)

# AADDS Instance
# Ignore all mutable inputs so Pulumi never PATCHes this resource. Otherwise N *other*
# properties can still differ (replicaSets, notificationSettings, sku, …) and Azure will re-validate
# LDAPS and reject a PFX that expires in <30 days.
DOMAIN_SERVICE_IGNORE_ALL_MUTABLE_INPUTS = [
    "domainSecuritySettings",
    "ldapsSettings",
]
domain_service = azure_native.aad.DomainService(str(aadds_name),
    opts=pulumi.ResourceOptions(
        depends_on=[aadds_resource_group,aadds_subnet1],
        ignore_changes=DOMAIN_SERVICE_IGNORE_ALL_MUTABLE_INPUTS,
    ),
    resource_group_name=aadds_resource_group.name,
    domain_name=aadds_name,
    domain_service_name=aadds_name,
    replica_sets=[azure_native.aad.ReplicaSetArgs(
        subnet_id=aadds_subnet1.id,
    )],
    ldaps_settings=azure_native.aad.LdapsSettingsArgs(
        external_access="Enabled",
        ldaps="Enabled",
        pfx_certificate=pfx_cert_string,
        pfx_certificate_password=pfx_pw,
    ),
    domain_security_settings=azure_native.aad.DomainSecuritySettingsArgs(
        channel_binding="Disabled",
        ldap_signing="Disabled",
        tls_v1="Disabled",
        ntlm_v1="Enabled",
        sync_ntlm_passwords="Enabled",
        sync_on_prem_passwords="Enabled",
        kerberos_rc4_encryption="Enabled",
        kerberos_armoring="Enabled",
    ),
    filtered_sync="Disabled",
    domain_configuration_type="FullySynced",
    sync_scope="All",
    notification_settings=azure_native.aad.NotificationSettingsArgs(
        notify_dc_admins="Enabled",
        notify_global_admins="Enabled",
    ),
    sku="Standard",
)

######################## Second Group Of Resources ########################
# Create the management server NIC and VM.

# Management Server 01 Network Interface
ms01_network_interface = azure_classic.network.NetworkInterface(str(ms01_vm_name) + "-eth0",
    opts=pulumi.ResourceOptions(depends_on=[aadds_ms_subnet1]),
    name=str(ms01_vm_name) + "-eth0",
    location=aadds_ms_vm_resource_group.location,
    resource_group_name=aadds_ms_vm_resource_group.name,
    ip_configurations=[azure_classic.network.NetworkInterfaceIpConfigurationArgs(
        name="ipconfig-mgmt",
        primary=True,
        subnet_id=aadds_ms_subnet1.id,
        private_ip_address_allocation="Dynamic",
        private_ip_address_version="IPv4")],        
    accelerated_networking_enabled=True,
    ip_forwarding_enabled=True,
)

# Management Server 01 VM
ms01_virtual_machine = azure_classic.compute.VirtualMachine(str(ms01_vm_name),
    opts=pulumi.ResourceOptions(depends_on=[ms01_network_interface]),
    name=str(ms01_vm_name),
    location=aadds_ms_vm_resource_group.location,
    resource_group_name=aadds_ms_vm_resource_group.name,
    network_interface_ids=[ms01_network_interface],
    primary_network_interface_id=ms01_network_interface,
    vm_size="Standard_D2s_v3",
    storage_image_reference=azure_classic.compute.VirtualMachineStorageImageReferenceArgs(
        publisher="MicrosoftWindowsServer",
        offer="WindowsServer",
        sku="2022-datacenter-azure-edition",
        version="latest",
    ),
    storage_os_disk=azure_classic.compute.VirtualMachineStorageOsDiskArgs(
        name=str(ms01_vm_name) + "_OsDisk_1",
        caching="ReadWrite",
        create_option="FromImage",
        managed_disk_type="StandardSSD_LRS",
        disk_size_gb=127,
        os_type = "Windows",
    ),
    os_profile=azure_classic.compute.VirtualMachineOsProfileArgs(
        computer_name=str(ms01_vm_name),
        admin_username=str(ms01_admin_username),
        admin_password=pulumi.Output.secret(ms01_admin_pw),
    ),
    os_profile_windows_config=azure_classic.compute.VirtualMachineOsProfileWindowsConfigArgs(
        provision_vm_agent=True,
        enable_automatic_upgrades=False,

    ),
    license_type="Windows_Server",
)

######################## Third Group Of Resources ########################
# Create route tables, associations, and VNET peerings.

# AADDS MS VNET Route Table
aadds_ms_route_table = azure_native.network.RouteTable(str(rg_prefix) + "-MS-VNET-to-FW",
    opts=pulumi.ResourceOptions(depends_on=[aadds_ms_subnet1,aadds_subnet1]),
    route_table_name=str(rg_prefix) + "-MS-VNET-to-FW",
    location=aadds_ms_networking_resource_group.location,
    resource_group_name=aadds_ms_networking_resource_group.name,
    disable_bgp_route_propagation=False,
    routes=build_routes(
        route_tables_cfg["AaddsMsToFw"],
        cfg,
        trust_nic_private_ip,
        {
            "hub_vnet_space": hub_vnet_cidr,
            "hub_vnet_cidr": hub_vnet_cidr,
        },
    ),
)

# Associate AADDS MS Route Table
aadds_ms_route_table_association_subnet1 = azure_classic.network.SubnetRouteTableAssociation(str(rg_prefix) + "-MS-VNET-to-FW-Association",
    opts=pulumi.ResourceOptions(depends_on=[aadds_ms_route_table]),
    subnet_id=aadds_ms_subnet1.id,
    route_table_id=aadds_ms_route_table.id)

# Create VNET peerings from stack config.
local_vnet_map = {
    "aadds_ms": {
        "resource_group_name": aadds_ms_networking_resource_group.name,
        "virtual_network_name": aadds_ms_virtual_network.name,
    },
    "aadds": {
        "resource_group_name": aadds_networking_resource_group.name,
        "virtual_network_name": aadds_virtual_network.name,
    },
}
remote_vnet_id_map = {
    "hub": hub_vnet_resource_id,
    "aadds_ms": aadds_ms_virtual_network.id,
    "aadds": aadds_virtual_network.id,
}
vnet_peerings = []
for peering in peerings_cfg:
    local_vnet_key = peering["local_vnet_ref"]
    local_cfg = local_vnet_map[local_vnet_key]
    remote_id = peering.get("remote_vnet_id")
    if not remote_id:
        remote_id = remote_vnet_id_map[peering["remote_vnet_ref"]]
    vnet_peerings.append(
        azure_native.network.VirtualNetworkPeering(
            peering["name"],
            opts=pulumi.ResourceOptions(
                depends_on=[aadds_ms_virtual_network, aadds_virtual_network],
                ignore_changes=["peeringSyncLevel", "remoteVirtualNetworkAddressSpace"],
            ),
            virtual_network_peering_name=peering["name"],
            allow_forwarded_traffic=bool(peering.get("allow_forwarded_traffic", True)),
            allow_gateway_transit=bool(peering.get("allow_gateway_transit", False)),
            allow_virtual_network_access=bool(peering.get("allow_virtual_network_access", True)),
            remote_virtual_network=azure_native.network.SubResourceArgs(id=remote_id),
            resource_group_name=local_cfg["resource_group_name"],
            use_remote_gateways=bool(peering.get("use_remote_gateways", False)),
            virtual_network_name=local_cfg["virtual_network_name"],
        )
    )

######################## Outputs ########################
pulumi.export("AADDS Domain Name", aadds_name)
pulumi.export(str(rg_prefix) + "-MS-VNET", aadds_ms_vnet)
pulumi.export(str(rg_prefix) + "-MS-1", aadds_ms_subnet1_space)
pulumi.export(str(rg_prefix) + "-MS-2", aadds_ms_subnet2_space)
pulumi.export(str(rg_prefix) + "-VNET", aadds_vnet)
pulumi.export(str(rg_prefix) + "-1", aadds_subnet1_space)
pulumi.export(str(rg_prefix) + "-2", aadds_subnet2_space)
pulumi.export("Management Server 01 VM Name", ms01_vm_name)
pulumi.export("Management Server Private IP", ms01_network_interface.private_ip_address)
pulumi.export("Key Vault Name", key_vault_name)