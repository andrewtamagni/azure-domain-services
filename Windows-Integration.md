# Windows Integration

Quick reference for using this repo with **Windows**, **Ubuntu WSL**, and **Docker Desktop** together.

---

## 1. Docker Desktop + Ubuntu WSL

Use one Docker engine (Docker Desktop) from both Windows and Ubuntu—no separate Docker install in WSL.

### Install / confirm

- **Docker Desktop for Windows**: [Install](https://docs.docker.com/desktop/install/windows-install/) and ensure it uses the **WSL 2** engine (Settings → General → “Use the WSL 2 based engine”).
- **Ubuntu WSL**: `wsl --install -d Ubuntu` (if not already installed). List distros: `wsl -l -v`.

### WSL integration (required)

So that `docker` in Ubuntu uses Docker Desktop’s engine:

1. Open **Docker Desktop** → **Settings** (gear).
2. Go to **Resources** → **WSL Integration**.
3. Turn **on** “Enable integration with my default WSL distro”.
4. Turn **on** the switch for **Ubuntu** (and any other distros you use).
5. Click **Apply & Restart**.

After that, from Ubuntu (`wsl -d Ubuntu` or `wsl`), `docker run hello-world` should work without installing Docker inside WSL.

### Optional: Ubuntu as default WSL distro

```powershell
wsl --set-default Ubuntu
```

Then `wsl` opens Ubuntu by default.

---

## 2. Git: same repo on Windows and Ubuntu WSL

When the repo lives on a **Windows drive** (e.g. `S:\GitHub\azure-pa-hub-network`) and you use it from both Windows and Ubuntu (e.g. `/mnt/s/GitHub/azure-pa-hub-network`), Git can show false “all files changed” or line-ending noise. Fix it once per repo from **Ubuntu**:

```bash
cd /mnt/s/GitHub/azure-pa-hub-network   # or your repo path in WSL

# Ignore executable-bit differences (fixes “all files modified” on WSL)
git config core.fileMode false

# Normalize line endings for WSL (optional but recommended)
git config core.autocrlf input
```

Then `git status` should match what you see on Windows, and the repo works consistently in both Windows and Ubuntu WSL with Docker Desktop.

---

## 3. Python: `python` → Python 3 (repo requirement)

This repo expects **`python`** to run Python 3 (e.g. `python stack_menu.py`, `python get_next_onprem_net.py`). On Ubuntu WSL, install:

```bash
sudo apt update && sudo apt install -y python-is-python3
```

That makes `python` a symlink to `python3` so the above commands work without changing scripts.

---

## 4. Quick reference

| Task                    | Command / location |
|-------------------------|--------------------|
| List WSL distros        | `wsl -l -v`        |
| Set Ubuntu default      | `wsl --set-default Ubuntu` |
| Docker WSL integration  | Docker Desktop → Settings → Resources → WSL Integration → enable Ubuntu |
| Git repo OK in both     | In repo (from WSL): `git config core.fileMode false` and optionally `git config core.autocrlf input` |
| `python` → Python 3     | Ubuntu: `sudo apt install -y python-is-python3` |
