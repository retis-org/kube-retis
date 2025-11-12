# Automated RETIS Collection (ARC)

A Python script for running RETIS network packet collection on Kubernetes worker nodes with advanced filtering capabilities using a modern, Kubernetes-native approach.

## 🎯 Overview

This tool automates the deployment and execution of RETIS (Real-time Traffic Inspection System) on Kubernetes worker nodes using a **pure Kubernetes-native implementation**. It provides flexible node selection through name patterns and workload filtering, making it easy to target specific nodes for network analysis. The tool uses privileged debug pods and the Kubernetes API directly - **no OpenShift CLI tools required**.

## ✨ Features

- **🔍 Smart Node Filtering**: Filter nodes by glob patterns (`worker-2*`) or running workloads
- **🚀 Parallel Execution**: Run RETIS collection on multiple nodes simultaneously
- **⚙️ Flexible Configuration**: Support for custom RETIS images, tags, and working directories
- **🛡️ Enhanced Safety**: Default dry-run mode for collection, explicit start required
- **📋 Comprehensive Management**: Start, stop, reset-failed, and download operations
- **📥 Results Download**: Automatically download events.json files from all nodes
- **🔧 Custom Commands**: Full control over RETIS commands and arguments
- **🏷️ Version Control**: Configurable RETIS version tags (defaults to stable v1.5.2)
- **🔌 Native Kubernetes Integration**: Uses Kubernetes Python client API for reliable cluster interaction
- **🐳 Modern Debug Pod Architecture**: Creates privileged debug pods with proper security contexts
- **🧹 Automatic Cleanup**: Smart pod lifecycle management with automatic resource cleanup
- **⚡ No CLI Dependencies**: Pure Python/Kubernetes API implementation - no `oc` or `kubectl` required

## 📋 Requirements

- Python 3.6+
- Access to a Kubernetes cluster (works with any Kubernetes distribution including OpenShift)
- Valid kubeconfig file
- Appropriate RBAC permissions to:
  - List and read nodes and pods
  - Create and delete pods in the target namespace (default: `default`)
  - Execute commands in pods (`pods/exec`)
- **No CLI tools required** - Pure Python implementation using Kubernetes API

## 🚀 Installation

1. **Clone or download the script:**
   ```bash
   git clone <repository-url>
   cd automated_retis_collection
   ```

2. **Create and activate a virtual environment (recommended):**
   ```bash
   # Create virtual environment
   python3 -m venv venv
   
   # Activate virtual environment
   # On Linux/macOS:
   source venv/bin/activate
   
   # On Windows:
   # venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
   or install core dependencies manually:
   ```bash
   pip install "kubernetes>=30.0.0" "urllib3>=2.0.0"
   ```
   
   For RETIS analysis features, also install:
   ```bash
   pip install "retis>=1.6.0"
   ```

## 🎮 Usage

### Basic Usage

```bash
# Preview RETIS collection (default dry-run mode)
python3 kube-retis.py --kubeconfig ~/.kube/config --node-filter "worker-2*" --retis-command "collect -o events.json -f 'tcp port 80'"

# Actually execute RETIS collection (use --start)
python3 kube-retis.py --kubeconfig ~/.kube/config --node-filter "worker-2*" --retis-command "collect -o events.json -f 'tcp port 80'" --start

# Filter nodes running specific workloads
python3 kube-retis.py --kubeconfig ~/.kube/config --workload-filter "ovn" --retis-command "collect -o network.json -f 'tcp'" --start

# Combine both filters
python3 kube-retis.py --kubeconfig ~/.kube/config --node-filter "worker*" --workload-filter "networking" --retis-command "collect -o net.json -f 'tcp'" --start

# Use without kubeconfig argument (will prompt)
python3 kube-retis.py --node-filter "worker-2*" --retis-command "collect -o events.json -f 'tcp port 443'" --start
```

### Advanced Usage

```bash
# Custom RETIS image and version (repository and tag specified separately)
python3 kube-retis.py --kubeconfig ~/.kube/config --retis-image "registry.example.com/retis" --retis-tag "v1.6.0" --retis-command "collect -o events.json -f 'tcp port 80'" --start

# Custom working directory
python3 kube-retis.py --kubeconfig ~/.kube/config --working-directory "/tmp/retis" --retis-command "collect -o events.json -f 'tcp'" --start

# Parallel execution on multiple nodes
python3 kube-retis.py --kubeconfig ~/.kube/config --node-filter "worker*" --retis-command "collect -o events.json -f 'tcp'" --parallel --start

# Explicit dry run (redundant with default, but clear)
python3 kube-retis.py --kubeconfig ~/.kube/config --node-filter "worker*" --retis-command "collect -o events.json -f 'tcp'" --dry-run

# Custom RETIS command (full control) - MUST include -o parameter
python3 kube-retis.py --kubeconfig ~/.kube/config --retis-command "collect -o custom.json --max-events 5000" --start

# RETIS profile command (different from collect)
python3 kube-retis.py --kubeconfig ~/.kube/config --retis-command "profile -o profile.json -t 30" --start

# Utility operations (no --retis-command needed)
python3 kube-retis.py --kubeconfig ~/.kube/config --node-filter "worker*" --stop
python3 kube-retis.py --kubeconfig ~/.kube/config --node-filter "worker*" --reset-failed
python3 kube-retis.py --kubeconfig ~/.kube/config --node-filter "worker*" --download-results
python3 kube-retis.py --kubeconfig ~/.kube/config --node-filter "worker*" --download-results --output-file custom.json
```

### Utility Operations (Execute Immediately)

```bash
# Stop RETIS collection (executes immediately, no --start needed)
python3 kube-retis.py --kubeconfig ~/.kube/config --node-filter "worker*" --stop

# Stop with parallel execution
python3 kube-retis.py --kubeconfig ~/.kube/config --stop --parallel

# Reset failed RETIS units
python3 kube-retis.py --kubeconfig ~/.kube/config --reset-failed

# Download all events.json files from nodes
python3 kube-retis.py --kubeconfig ~/.kube/config --download-results

# Preview utility operations (use --dry-run)
python3 kube-retis.py --kubeconfig ~/.kube/config --stop --dry-run
python3 kube-retis.py --kubeconfig ~/.kube/config --download-results --dry-run
```

## 📖 Command Line Arguments

### Core Options
| Argument | Short | Description | Default |
|----------|--------|-------------|---------|
| `--kubeconfig` | `-k` | Path to kubeconfig file | Prompts if not provided |
| `--node-filter` | `-n` | Glob pattern to filter nodes by name (`worker-2*`) | None |
| `--workload-filter` | `-w` | Regex pattern to filter nodes by workload | None |
| `--dry-run` | | Show commands without executing (default for collection) | Collection: True, Utils: False |
| `--start` | | Actually execute RETIS collection (overrides dry-run) | False |
| `--parallel` | | Run on all nodes in parallel | False (sequential) |

### RETIS Configuration
| Argument | Short | Description | Default |
|----------|--------|-------------|---------|
| `--retis-image` | | RETIS container image repository (without tag) | `quay.io/retis/retis` |
| `--retis-tag` | | RETIS version tag to use | `v1.5.2` |
| `--retis-command` | | **REQUIRED for collection**: Complete RETIS command string. Must include `-o <filename>`. Not needed for utility operations. | None |
| `--output-file` | `-o` | Output file name for utility operations (e.g., --download-results) | `events.json` |
| `--working-directory` | | Working directory for RETIS collection | `/var/tmp` |

### Operations
| Argument | Description | Execution |
|----------|-------------|-----------|
| `--stop` | Stop RETIS collection on filtered nodes | Immediate |
| `--reset-failed` | Reset failed RETIS systemd units | Immediate |
| `--download-results` | Download events.json files from nodes | Immediate |

## 🔍 Filtering Options

### Node Name Filtering (`--node-filter`)

Filter nodes using **glob patterns** (shell-style wildcards) on node names:

```bash
# Match nodes starting with "worker-2"
--node-filter "worker-2*"

# Match all worker nodes
--node-filter "worker*"

# Match specific worker numbers
--node-filter "worker-[12]*"

# Match compute nodes
--node-filter "*compute*"
```

**Glob Pattern Syntax:**
- `*` - Matches any characters
- `?` - Matches any single character  
- `[abc]` - Matches any character in brackets
- `[a-z]` - Matches any character in range

### Workload Filtering (`--workload-filter`)

Filter nodes based on workloads (pods) running on them:

```bash
# Nodes running OVN networking components
--workload-filter "ovn"

# Nodes running nginx workloads
--workload-filter "nginx"

# Nodes in specific namespaces
--workload-filter "kube-system"

# Complex patterns
--workload-filter "app=frontend"
```

The workload filter searches through:
- Pod names
- Pod namespaces  
- Pod labels (as `key=value` pairs)

## 🛡️ Safety Features

### Interactive Confirmation
When no filters are specified, the script will:
1. Warn that it will run on ALL worker nodes
2. Prompt for confirmation (unless in dry-run mode)
3. Allow cancellation

### Dry Run Mode
Use `--dry-run` to:
- See which nodes would be selected
- Preview commands that would be executed
- Test filters without making changes

## 📝 Examples

### Example 1: Target Specific Worker Nodes
```bash
python3 kube-retis.py --kubeconfig ~/.kube/config --node-filter "worker-0[1-3]"
```

### Example 2: Find Nodes Running OVN Components
```bash
python3 kube-retis.py --kubeconfig ~/.kube/config --workload-filter "ovn-kubernetes"
```

### Example 3: Combined Filtering with Parallel Execution
```bash
python3 kube-retis.py \
  --kubeconfig ~/.kube/config \
  --node-filter "compute" \
  --workload-filter "networking" \
  --parallel \
  --dry-run
```

### Example 4: Stop RETIS on All Worker Nodes
```bash
python3 kube-retis.py --kubeconfig ~/.kube/config --stop --parallel
```

### Example 5: Download Results from Specific Nodes
```bash
python3 kube-retis.py --kubeconfig ~/.kube/config --node-filter "worker-2*" --download-results
```

### Example 6: Custom RETIS Command
```bash
python3 kube-retis.py --kubeconfig ~/.kube/config --retis-command "profile -o network-profile.json -t 60" --start
```

## 🚀 Major Features

### 🛡️ Safe-by-Default Behavior

**RETIS Collection Operations** default to **dry-run mode** for safety:
```bash
# Safe: Previews what would be executed
python3 kube-retis.py --kubeconfig ~/.kube/config --node-filter "worker*"

# Explicit: Actually executes collection
python3 kube-retis.py --kubeconfig ~/.kube/config --node-filter "worker*" --start
```

**Utility Operations** execute immediately as expected:
```bash
# These execute immediately (no --start needed)
python3 kube-retis.py --kubeconfig ~/.kube/config --stop
python3 kube-retis.py --kubeconfig ~/.kube/config --reset-failed  
python3 kube-retis.py --kubeconfig ~/.kube/config --download-results

# Use --dry-run to preview utility operations
python3 kube-retis.py --kubeconfig ~/.kube/config --stop --dry-run
```

### 📥 Automated Results Download

Download all `events.json` files from filtered nodes with automatic naming:

```bash
# Download from all nodes (default: events.json)
python3 kube-retis.py --kubeconfig ~/.kube/config --download-results

# Download from specific nodes only
python3 kube-retis.py --kubeconfig ~/.kube/config --node-filter "worker-2*" --download-results

# Download specific output file name
python3 kube-retis.py --kubeconfig ~/.kube/config --output-file "trace.json" --download-results
```

**File Naming**: Files are saved as `{node-short-name}_{output-file}` to prevent overwrites:
- `worker-0_events.json`
- `worker-1_events.json` 
- `worker-2_events.json`

### 🔧 Custom RETIS Commands

Take full control over RETIS execution with `--retis-command`:

```bash
# Custom collect command (must include -o parameter)
python3 kube-retis.py --kubeconfig ~/.kube/config --retis-command "collect -o custom.json --max-events 5000" --start

# RETIS profile instead of collect
python3 kube-retis.py --kubeconfig ~/.kube/config --retis-command "profile -o profile.json -t 30" --start

# Complex command with multiple options
python3 kube-retis.py --kubeconfig ~/.kube/config --retis-command "collect -o trace.json --allow-system-changes --filter-packet 'tcp port 443' --max-events 10000" --start
```

**Override Behavior**: When using `--retis-command`, individual RETIS parameters are ignored with warnings.

### 🏷️ Version Control

Control RETIS version with `--retis-tag`:

```bash
# Use specific version (default: v1.5.2)
python3 kube-retis.py --kubeconfig ~/.kube/config --retis-tag "v1.6.0" --start

# Use latest version
python3 kube-retis.py --kubeconfig ~/.kube/config --retis-tag "latest" --start

# Use development version
python3 kube-retis.py --kubeconfig ~/.kube/config --retis-tag "main" --start
```

### 🔄 System Maintenance

Reset failed systemd units across nodes:

```bash
# Reset failed units on all nodes
python3 kube-retis.py --kubeconfig ~/.kube/config --reset-failed

# Reset on specific nodes
python3 kube-retis.py --kubeconfig ~/.kube/config --node-filter "worker*" --reset-failed

# Preview reset operation
python3 kube-retis.py --kubeconfig ~/.kube/config --reset-failed --dry-run
```

## 🔧 Technical Details

### Node Selection Logic
1. **Worker Node Detection**: Automatically excludes master/control-plane nodes
2. **Name Filtering**: Applied first using glob pattern matching (`fnmatch`)
3. **Workload Filtering**: Applied second by checking pods on remaining nodes
4. **Final Validation**: Ensures at least one node matches before proceeding

### Debug Pod Architecture
ARC uses a modern **KubernetesDebugPodManager** that creates privileged debug pods with:
- **Security Context**: Privileged access with `SYS_ADMIN`, `NET_ADMIN`, and `SYS_PTRACE` capabilities
- **Host Access**: Full host filesystem mounted at `/host`, host PID and network namespaces
- **Tolerations**: Can run on any node including control-plane nodes
- **Context Management**: Automatic pod lifecycle with cleanup on completion

### RETIS Collection Process
1. **Script Download**: Downloads `retis_in_container.sh` to local temp file
2. **Debug Pod Creation**: Creates privileged debug pod on target node using Kubernetes API
3. **File Transfer**: Copies script to node via debug pod using base64 encoding
4. **Execution**: Runs RETIS using systemd-run through debug pod with chroot access
5. **Monitoring**: Checks systemd unit status for success/failure via debug pod
6. **Cleanup**: Automatically removes debug pods and temporary files

### Debug Pod Operations
- **Command Execution**: Uses Kubernetes `stream` API for real-time command execution
- **File Operations**: Secure file transfer using base64 encoding through pod exec
- **Chroot Support**: All host operations use `chroot /host` for proper system access
- **Error Handling**: Robust exception handling with detailed error reporting

### Error Handling
- **Kubernetes API Errors**: Graceful handling of connection and permission issues
- **Node Access Errors**: Individual node failures don't stop other nodes
- **Pod Creation Failures**: Automatic cleanup and detailed error reporting
- **Timeout Protection**: All operations have configurable timeout limits
- **Resource Cleanup**: Debug pods and temporary files are always cleaned up

## 🚨 Troubleshooting

### Common Issues

**"No module named 'kubernetes'"**
```bash
pip install "kubernetes>=30.0.0" "urllib3>=2.0.0"
```

**"Failed to connect to Kubernetes cluster"**
- Verify kubeconfig file exists and is valid
- Check cluster connectivity
- Ensure proper RBAC permissions

**"No nodes found matching filters"**
- Verify glob patterns are correct (use `*` for wildcards, not regex)
- Check that target nodes exist and are workers
- Use dry-run mode to test filters (default for collection operations)

**"Warning: Individual RETIS parameters are ignored when using --retis-command"**
- This is expected when using `--retis-command` with other RETIS options
- The custom command takes full precedence

**"No results files found for download"**
- Ensure RETIS collection has completed successfully 
- Check that the output file exists on target nodes
- Verify working directory and file names

**"Failed to create debug pod" or permission errors**
- Ensure RBAC permissions include pod creation and execution in target namespace
- Check if the namespace exists (default: `default`)
- Verify cluster has sufficient resources for debug pods

**"Debug pod creation timeout"**
- Check node availability and resource constraints
- Verify image registry access (default: `registry.redhat.io/ubi8/ubi:latest`)
- Increase timeout if needed for slow clusters

**"Error: invalid reference format"**
- This usually means the image reference is malformed
- Common cause: including tag in `--retis-image` (e.g., `--retis-image quay.io/retis/retis:latest`)
- **Correct usage**: `--retis-image quay.io/retis/retis --retis-tag latest`
- The script will auto-fix this and show a warning

### Debug Tips

1. **Default Preview Mode**: Collection operations preview by default (no `--dry-run` needed)
2. **Test Filters**: Use glob patterns like `worker-2*` instead of regex `^worker-2`
3. **Start Small**: Test with a single node first: `--node-filter "worker-0*"`
4. **Use Start Flag**: Remember to add `--start` to actually execute collection
5. **Check Connectivity**: Ensure kubeconfig and cluster access work
6. **Verify Permissions**: Ensure RBAC permissions for nodes and pods access

## 🔄 Recent Changes

### 🚀 Version 3.0 - Kubernetes-Native Debug Pod Architecture

#### 🐳 Major Architecture Overhaul:
- **KubernetesDebugPodManager**: Brand new debug pod manager class for Kubernetes-native operations
- **Privileged Debug Pods**: Creates secure debug pods with proper security contexts and capabilities
- **Host Access**: Full host filesystem access via volume mounts and chroot operations
- **Stream API Integration**: Real-time command execution using Kubernetes stream API
- **Automatic Cleanup**: Smart pod lifecycle management with context managers

#### 🔧 Technical Improvements:
- **Zero CLI Dependencies**: Completely eliminated OpenShift CLI (`oc`) requirements
- **Pure Kubernetes API**: Direct API calls for all operations (pod creation, exec, file transfer)
- **Enhanced Security**: Proper RBAC requirements and security context configuration
- **Better Error Handling**: Comprehensive exception handling for pod operations
- **Resource Management**: Automatic debug pod cleanup and resource management

#### ⚡ Performance & Reliability:
- **Faster Operations**: Direct API calls eliminate subprocess overhead
- **Better Concurrency**: Native support for parallel pod operations
- **Improved Timeout**: Configurable timeouts for all pod operations
- **Robust File Transfer**: Secure base64-encoded file transfer via pod exec

### 🚀 Version 2.0 - Major Feature Updates

#### ✨ Previous Features Added:
- **🛡️ Safe-by-Default**: RETIS collection defaults to dry-run mode, requires `--start` to execute
- **📥 Results Download**: `--download-results` operation to fetch all events.json files
- **🔧 Custom Commands**: `--retis-command` option for full control over RETIS arguments
- **🏷️ Version Control**: `--retis-tag` option to specify RETIS version (default: v1.5.2)
- **🔄 System Maintenance**: `--reset-failed` operation to reset failed systemd units
- **🔍 Node Filtering**: Glob patterns (`worker-2*`) for intuitive node matching

#### 🛡️ Enhanced Safety:
- **Default Dry-Run**: Collection operations preview by default, utility operations execute immediately
- **Smart Behavior**: Stop, reset-failed, and download operations work without `--start` flag
- **Clear Warnings**: Alerts when conflicting options are used together

### 📈 Backward Compatibility:
- All existing functionality preserved
- Same core command-line interface
- Enhanced with new optional features
- Improved safety with default dry-run mode

## 📄 License

This project is provided as-is for educational and operational purposes.

## 🤝 Contributing

Contributions, issues, and feature requests are welcome!

## 📞 Support

For issues related to:
- **RETIS**: See [RETIS documentation](https://github.com/retis-org/retis)
- **Kubernetes**: Consult Kubernetes documentation and cluster-specific guides
- **Kubernetes Python Client**: Check [official Kubernetes Python client documentation](https://github.com/kubernetes-client/python)
- **Debug Pods**: Review Kubernetes pod security policies and RBAC configuration
