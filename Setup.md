# Setup guide

How to install and run this project on your own machine, from scratch.

## 1. What you need

| Item | Version | Notes |
|---|---|---|
| Linux (Ubuntu) | The project was developed on **Ubuntu 26.04** in VirtualBox. Ubuntu 22.04 or 24.04 should also work. | Mininet and Open vSwitch need Linux. A VM is fine. |
| VM resources | 4 GB RAM, 2 CPUs, 20 GB disk | A starved VM distorts latency measurements |
| Open vSwitch | `<ovs-vsctl --version>` | From apt |
| Mininet | `<mn --version>` | From apt |
| iperf | **version 2** | Not iperf3 |
| System Python | 3.x (3.14 on the development VM) | Runs the app, topology, experiments and analysis |
| Ryu | **4.34** | Must run in a **Python 3.9** virtual environment |
| eventlet | **0.30.2** (pinned) | Newer versions break Ryu |
| setuptools | **below 68** (needed only to build Ryu) | |
| uv | any recent version | Used to create the Python 3.9 environment |
| matplotlib | any recent version | Analysis plots |
| tcpdump, curl, git | any | |

After installing, record your exact versions here (for the report):
```bash
lsb_release -a
python3 --version
ovs-vsctl --version
mn --version
iperf --version
~/ryu-venv39/bin/ryu-manager --version
```

## 2. Install system packages

On the Ubuntu machine, one package at a time (wait for each to finish):

```bash
sudo apt update
sudo apt install -y openvswitch-switch
sudo apt install -y mininet
sudo apt install -y iperf
sudo apt install -y tcpdump curl git
sudo apt install -y python3-pip python3-venv
```

If apt says "Could not get lock", an automatic update is running. Wait a few minutes; do not delete lock files.

Check Open vSwitch and Mininet:
```bash
sudo systemctl status openvswitch-switch     # should be active
sudo mn --test pingall                       # should show 0% dropped
sudo mn -c                                   # clean up afterwards
```

## 3. Install matplotlib (for the plots)

```bash
pip install matplotlib --break-system-packages
```
(Or `sudo apt install python3-matplotlib`.) Install it for the system Python, not the Ryu environment.

## 4. Install Ryu (the tricky part)

Ryu depends on an old `eventlet` that does not work with Python 3.10 or newer. Use a separate Python 3.9 virtual environment. The system Python and Mininet are not affected.

```bash
pip install uv --break-system-packages
uv venv --python 3.9 ~/ryu-venv39
source ~/ryu-venv39/bin/activate
python --version                               # must say 3.9.x
```

Then, with the environment active:
```bash
~/.local/bin/uv pip install "setuptools<68" wheel pbr
~/.local/bin/uv pip install --no-build-isolation ryu
~/.local/bin/uv pip install eventlet==0.30.2
ryu-manager --version                          # should print 4.34
```

If `uv` is not found, install it in the Ryu environment's parent shell with `pip install uv --break-system-packages` and call it by its full path (`which uv` shows it).

Notes:
- A `SyntaxWarning` about `is not` from Ryu on startup is harmless.
- An `EventletDeprecationWarning` is also harmless.
- Install eventlet **separately, after Ryu** (as shown) so the installer does not refuse the version pin.

## 5. Get the project

```bash
git clone https://github.com/<owner>/emergency-notification-sdn.git
cd emergency-notification-sdn
chmod +x sdn/qos_setup.sh
```

If you edited files on Windows, make sure shell scripts use LF line endings (VS Code status bar: click `CRLF`, choose `LF`).

Check `app/config.py`:
```python
SERVER_IP = "10.0.0.1"
CONTROLLER_URL = "http://10.0.0.254:8080"
BG_PORT = 5201
```

## 6. Verify the toolchain (5 minutes)

Open two terminals in the repository.

**Terminal 1** (controller; no sudo):
```bash
source ~/ryu-venv39/bin/activate
cd sdn
MODE=baseline ryu-manager controller.py
```
Expected: a line `=== MODE: baseline ===` and `wsgi starting up on http://0.0.0.0:8080`.

**Terminal 2:**
```bash
sudo mn -c
cd topology
sudo python3 topo.py
```
Expected: Terminal 1 prints `switch 1 connected` and `switch 2 connected`. At the `mininet>` prompt run `pingall`; it should show 0% dropped. Type `exit`, then `sudo mn -c`, and stop the controller with Ctrl+C.

## 7. Run the experiments

```bash
cd experiments
sudo python3 run_experiments.py --ryu ~/ryu-venv39/bin/ryu-manager --modes static --loads heavy --runs 1
sudo chown -R $USER ../results
python3 analyze.py ../results
```
This single quick run takes about a minute. Check that `results/static_heavy_run1/` contains `server_log.csv`, `sub1_log.csv`, `controller.log`, `iperf_client.txt`, `s1_queue-stats.txt` and `meta.json`.

Full set (about 20 minutes, do not use the machine meanwhile):
```bash
sudo python3 run_experiments.py --ryu ~/ryu-venv39/bin/ryu-manager --runs 3
sudo chown -R $USER ../results
python3 analyze.py ../results
```

## 8. Optional: edit from Windows with VS Code

Edit and run inside the VM; Windows only needs an SSH client.

1. In the VM: `sudo apt install openssh-server`, then check `systemctl status ssh` is active.
2. In VirtualBox (VM shut down or running): Settings, Network, Adapter 1 (NAT), Advanced, Port Forwarding. Add a rule: TCP, Host IP `127.0.0.1`, Host Port `2222`, Guest Port `22`.
3. On Windows, make sure `ssh` works in a terminal (Windows has an OpenSSH client; installing Git for Windows also provides one).
4. In VS Code, install the **Remote - SSH** extension, run "Remote-SSH: Connect to Host", add `ssh -p 2222 <vm-user>@127.0.0.1`, choose Linux, and enter the VM password.
5. Open the project folder on the VM and use VS Code's terminal there.

## 9. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Could not get lock /var/lib/dpkg/lock-frontend` | Automatic updates are running. Wait. |
| `ryu-manager: command not found` | Ryu environment not active. `source ~/ryu-venv39/bin/activate`. |
| `cannot import name 'ALREADY_HANDLED' from 'eventlet.wsgi'` | eventlet too new. Use eventlet 0.30.2 in a Python 3.9 environment. |
| `No module named 'imp'` | Python 3.12 or newer. Use the Python 3.9 environment. |
| `cannot set 'is_timeout' attribute of immutable type 'TimeoutError'` | Python 3.10 or newer with old eventlet. Use Python 3.9. |
| `get_script_args` error while installing Ryu | Build used a new setuptools. Install `setuptools<68` and use `--no-build-isolation`. |
| No `switch connected` in the controller log | Start the controller before the topology, and check Mininet points to 127.0.0.1:6653. |
| `pingall` shows packet loss | Look for `OpenFlow error` lines in the controller output. |
| Alerts never reach subscribers | Check `SERVER_IP`, run `pingall` first, start the server before subscribers. |
| No priority effect | Apply `qos_setup.sh` **after** the topology starts, and run `pingall` first. |
| Latency graph shows no difference | Judge with UDP alerts on port 5002; `ping` uses the best-effort queue. |
| `Address already in use` (8080 or 6653) | An old controller is running. `pkill -f ryu-manager`. |
| Strange leftovers between runs | `sudo mn -c` |
| `qos_setup.sh` fails with odd characters | CRLF line endings. Convert to LF. |
| Experiment `FAILED: controller died` | Read `controller.log` in that run's folder. |

## 10. Tear-down

```bash
sudo mn -c
pkill -f ryu-manager
```