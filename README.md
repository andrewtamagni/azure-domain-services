# Azure Domain Services (Pulumi)

Pulumi project for **Microsoft Entra Domain Services** and its management network: dedicated resource groups, split **/25** VNets derived from one **/24** CIDR, subnet NSGs, AADDS instance with external LDAPS, one management VM, route table for the management subnet, and VNet peerings to hub and between AADDS VNets.

**AADDS naming:** Historically the product was **Azure Active Directory Domain Services**, and **AADDS** was the usual acronym. Microsoft rebranded it to **[Microsoft Entra Domain Services](https://learn.microsoft.com/en-us/entra/identity/domain-services/)**; you will still see **AADDS** in ARM resource types, older docs, and many examples. This stack keeps **`aadds_*`** keys and resource prefixes for continuity with that history—they refer to Entra Domain Services.

All deployment secrets (LDAPS PFX cert material and VM admin password) are read from **Azure Key Vault** at deploy time.

### Naming and topology (MS vs domain VNet)

The **`MS`** suffix on resource groups and resources (for example **`-MS-Networking`**, **`-MS-VNET`**, **`-MS-NSG`**) means **management server**: a separate footprint for the management VM and its VNet, not the Entra Domain Services VNet itself.

- **`-MS-VNET`** is peered **directly to the hub** so administrators reach the management subnet from the corporate network (and routes can use the firewall trust IP as next hop).
- The **domain services VNet** (`{rg_prefix}-VNET`) is **not** peered to the hub in this design; it is peered **only to `-MS-VNET`** so AADDS traffic stays isolated from direct hub peering while still reachable for operations.

**Purpose of `-MS-VNET`:** host the management server VM (RSAT-capable Windows Server). After deployment you join that VM to the **Entra Domain Services** domain and use **Active Directory Users and Computers** and related tools to administer the managed domain—without placing management tooling on the same VNet as the domain controllers.

---

## Run with Docker

You need [Docker](https://docs.docker.com/get-docker/), a [Pulumi access token](https://www.pulumi.com/docs/pulumi-cloud/access-tokens/) (or `PULUMI_ENV_FILE` with `PULUMI_ACCESS_TOKEN=...`), and this repo’s **`Pulumi.yaml`**, **`Dockerfile`**, and **`requirements.txt`**. Do **not** set `virtualenv: venv` in **`Pulumi.yaml`** — the helper scripts refuse to run if it is set.

**Set the token on the host before** you run **`docker_pulumi_shell.sh`** or **`win_docker_pulumi_shell.bat`**, so the shell script can pass **`PULUMI_ACCESS_TOKEN`** into the container. Replace the placeholder with your real token.

PowerShell:

```powershell
$env:PULUMI_ACCESS_TOKEN = "pul-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
```

Bash (Linux, macOS, WSL):

```bash
export PULUMI_ACCESS_TOKEN=pul-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
```

**`docker_pulumi_shell.sh`** passes **`HOST_UID`** / **`HOST_GID`** from your Linux or macOS user into the container so **`stack_menu.py`** can **`chown`** new **`Pulumi.<stack>.yaml`** files on the bind-mounted repo to you (and set mode **`0644`**). On Windows **cmd**, set `set HOST_UID=...` and `set HOST_GID=...` before **`win_docker_pulumi_shell.bat`** if your stack files end up owned by root, or fix ownership once with **`sudo chown`** on WSL.

**Linux / macOS / WSL** — build the image and open a shell in `/app`:

```bash
cd /path/to/azure-domain-services
chmod +x docker_pulumi_shell.sh    # once
export PULUMI_ACCESS_TOKEN="pul-xxxx"
./docker_pulumi_shell.sh
```

**Examples:** build only — `./docker_pulumi_shell.sh --build-only`. Token in a file — `export PULUMI_ENV_FILE="$HOME/.pulumi-env"` then run the script. All flags — `./docker_pulumi_shell.sh --help`.

The image is tagged **`pulumi/azure-domain-services`** (see **`Dockerfile`**).

**Windows (PowerShell)** — from the repo directory with Docker Desktop running:

```bat
$env:PULUMI_ACCESS_TOKEN = "pul-xxxx"
win_docker_pulumi_shell.bat
```

For WSL, Git, and line endings on Windows drives, see **`Windows-Integration.md`**. To run without Docker: `pip install -r requirements.txt` on the host.

---

## Create a new stack

From repo root, initialize/select a stack and fill in **`Pulumi.<stack>.yaml`**.
Recommended path is **`python stack_menu.py`** (guided checks + seeding from sample).

### Menu seeding (this project)

| Project (`Pulumi.yaml` `name`) | `stack_menu.py` checklist / seed |
|--------------------------------|-----------------------------------|
| **azure-domain-services** (this repo) | **`Pulumi.sample.yaml`** (full committed example) and **`default_vars.yaml`** (required/optional key sketch). |

### What `stack_menu.py` does here (this repo)

- Treats **`Pulumi.sample.yaml`** as the shape/placeholder source and uses **`default_vars.yaml`** for required-vs-optional checks.
- **Create new stack** runs a domain-services-specific wizard for core fields, applies **`azure:subscriptionId`** / **`azure:tenantId`** from **`az account show`** when available, enforces **`aadds_vnet_space`** as **`/24`**, and seeds **`aadds_dns_servers`** as `.132` and `.133`. For **`aadds-pfx-cert-path`**, if multiple **`.pfx`** files exist in the repo directory, the menu lists them by number; if exactly one exists, it asks for confirmation before using it.
- For complete stacks, menu actions include adding **LDAP connections** (and appending matching **`aadds_nsg_rules`** entries with incremented rule name/priority).
- For this project, **peering** and **route-table** edits are done in YAML (or stack variables flow), not via direct add-one menu actions.

### Configure the stack

- **Recommended:** `python stack_menu.py` for checklist/merge and guided variable updates.
- **Manual:** copy **`Pulumi.sample.yaml`** to **`Pulumi.<stack>.yaml`** and replace placeholders.
- Required sections include **`pa_hub_stack`**, **`ldap_connections`**, **`aadds-pfx-cert-path`**, **`ms01_vm`**, **`key_vault`**, **`aadds_ms_nsg_rules`**, **`aadds_nsg_rules`**, **`route_tables`**, and **`peerings`**.
- **`aadds_vnet_space`** must be a **`/24`**; `stack_menu.py` seeds **`aadds_dns_servers`** as the **`.132`** and **`.133`** addresses in that range. **After deployment, verify those two IPs match your managed domain’s domain controllers:** in **Azure Portal**, open the **Microsoft Entra Domain Services** (legacy name **Azure AD Domain Services**) resource for your domain, then **Properties**. The **IP addresses** shown there are the DNS servers the VNets should use. If they differ from `.132` / `.133`, update **`aadds_dns_servers`** in **`Pulumi.<stack>.yaml`** and run **`pulumi up`** so custom DNS on the VNets stays correct.

Example:

```bash
az login
pulumi stack init dev
pulumi stack select dev
python stack_menu.py
python create_keyvault.py --check-only --stack dev
pulumi preview && pulumi up
```

---

## Deploy and destroy

```bash
pulumi preview
pulumi up
pulumi destroy
```

---

## Maintaining versions

When you refresh this project’s tooling, update these together so previews and deploys stay consistent:

- **`Dockerfile`**: bump the **`pulumi/pulumi-python`** image tag (currently **`pulumi/pulumi-python:3.220.0`**) to a current release from [Docker Hub — `pulumi/pulumi-python`](https://hub.docker.com/r/pulumi/pulumi-python/tags) so the image’s bundled Pulumi CLI matches your expectations.
- **`requirements.txt`**: review and update pinned **`pulumi`** and provider packages (for example **`pulumi-azure`**, **`pulumi-azure-native`**, and any other Python dependencies) so they remain compatible with that base image; then rebuild the Docker image (for example `./docker_pulumi_shell.sh --build-only` or your usual **`docker build`**).

If you run Pulumi on the host instead of Docker, align the installed **`pulumi`** CLI and **`pip install -r requirements.txt`** with the same versions where practical.

---

## Azure Key Vault

An **Azure Key Vault is required** for this stack to deploy. The Pulumi program reads **secrets** (PFX password, VM admin password) from the **Key Vault** you declare under **`key_vault`** in the stack config. The **LDAPS PKCS#12 file** is **not** stored in Key Vault: stack config **`aadds-pfx-cert-path`** points to a **`.pfx`** on the machine that runs **`pulumi up`** (paths relative to the repo root resolve against the program directory). The **Key Vault** is **outside this Pulumi stack**: it is **not** created or removed by **`pulumi up`** / **`pulumi destroy`** as part of the stack’s tracked resources. Treat it as shared Azure infrastructure in the subscription and don't forget to delete after it is no longer needed.

**You can reuse one Key Vault across many stacks** in the same subscription (for example hub and VM stacks) by pointing each stack’s **`key_vault.name`** (and related settings) at the same **Key Vault**, no need for a separate **Key Vault** per stack unless you want isolation.

**`create_keyvault.py`** reads **`key_vault`** from the **selected Pulumi stack’s** config (`Pulumi.<stack>.yaml`). When you run it without **`--check-only`**, it **creates the Key Vault resource group and the Key Vault itself** as needed according to that configuration, applies optional **`key_vault.iam_groups`** access, and walks through the secrets listed under **`key_vault.keys`**. See **`python create_keyvault.py --help`** for **`--stack`**, **`--check-only`**, and **`--yes`**. You can also drive this from **`stack_menu.py`**.

Required secret names for this project are listed below; the full **`key_vault`** shape is in **`Pulumi.sample.yaml`**.

### Key Vault secrets

| Secret | Used for |
|--------|----------|
| `aadds-pfx-password` | PFX password for the AADDS LDAPS certificate (must match the file at **`aadds-pfx-cert-path`**) |
| `aadds-ms01-admin-pw` | Local admin password for the MS01 management VM |

### LDAPS PFX file (`aadds-pfx-cert-path`)

**`__main__.py`** reads the **binary** **`.pfx`** from disk, verifies it opens with the Key Vault secret **`aadds-pfx-password`** (using the **`cryptography`** package), then **Base64-encodes** those bytes for the Azure LDAPS API. The same wire format as before, without storing the cert blob in Key Vault. Put **`.pfx`** files in the repo (or elsewhere) and set **`aadds-pfx-cert-path`** to a path **relative to the repo** or an **absolute** path. **`*.pfx`** is listed in **`.gitignore`** so the private key is not committed by mistake.

### Verify PFX (DNS names, thumbprint, expiration)

Before uploading LDAPS material, confirm the certificate matches the **FQDN** clients will use (typically **Subject Alternative Name** entries, or the legacy **CN** in the subject), note the **thumbprint** for your own records, and check **notBefore** / **notAfter** so it is not expired or about to expire.

Linux/macOS/WSL (**OpenSSL** — replace the password; omit **`-passin`** to be prompted instead):

```bash
openssl pkcs12 -legacy -in ./cert.pfx -clcerts -nokeys -passin 'pass:YOUR_PASSWORD'
```

If **`-ext subjectAltName`** errors on an older OpenSSL, drop that flag and use **`-text`**, then inspect the **Subject Alternative Name** block in the output.

PowerShell (**`Get-PfxData`** — prompts for the PFX password; requires **Windows PowerShell 5.1** or **PowerShell 7+** on Windows with the default **PKI** surface):

```powershell
$pfxPath = 'C:\path\to\cert.pfx'
$pfxPass = Read-Host 'PFX password' -AsSecureString
Get-PfxData -FilePath $pfxPath -Password $pfxPass | ForEach-Object {
  $_.EndEntityCertificates | ForEach-Object {
    [PSCustomObject]@{
      Subject    = $_.Subject
      DnsNames   = ($_.DnsNameList | ForEach-Object { $_.Unicode }) -join '; '
      Thumbprint = $_.Thumbprint
      NotBefore  = $_.NotBefore
      NotAfter   = $_.NotAfter
    }
  }
} | Format-List
```

---

## Network Config In Stack YAML

This repo now follows the same declarative pattern as your completed repos:

- **NSGs:** `aadds_ms_nsg_rules` and `aadds_nsg_rules`
- **Routes:** `route_tables.AaddsMsToFw`
- **Peerings:** `peerings` list with `local_vnet_ref` and either `remote_vnet_ref` or `remote_vnet_id`

**Hub-side peering:** Azure VNet peering uses two one-way links. This project creates only the outbound peerings from **this** stack’s virtual networks toward the remote VNet (for example the hub). For each pair to reach **Connected**, the **hub** stack (**azure-pa-hub-network**) must also define the matching return peering in its **`peerings`** configuration and you must run **`pulumi up`** there, or you must create that reverse link manually in Azure. Either direction may be created first.

Adjusting NSG rules, route entries, and peerings no longer requires editing Python source.

---

## Project layout (quick reference)

| Path | Role |
|------|------|
| **`__main__.py`** | AADDS resources; reads stack config + Key Vault; applies NSGs, routes, peerings from YAML. |
| **`stack_menu.py`** | Stack checklist and variable management. |
| **`create_keyvault.py`** | Key Vault bootstrap/check helper for this project. |
| **`Pulumi.sample.yaml`** | Full committed stack template. |
| **`default_vars.yaml`** | Required/optional key sketch for menu and docs parity. |

---

## Developed By

Andrew Tamagni (see file headers for history).

---

## AI Assistance Disclosure

Portions of this repository and documentation were developed with assistance from Cursor AI and have been reviewed by humans.
