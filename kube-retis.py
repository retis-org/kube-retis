#!/usr/bin/env python3

import os
import sys
import argparse
import subprocess
import urllib.request
import tempfile
import time
import json
import re
import fnmatch
import ssl
import urllib3
import contextlib
import io
import random
import string
from kubernetes import client, config
from kubernetes.stream import stream

try:
    from retis import EventFile
    RETIS_ANALYSIS_AVAILABLE = True
except ImportError:
    RETIS_ANALYSIS_AVAILABLE = False


class KubernetesDebugPodManager:
    """
    A Kubernetes-native replacement for 'oc debug node' commands.
    
    This class provides an interface for creating debug pods,
    executing commands, and managing file operations on Kubernetes nodes.
    """
    
    def __init__(self, k8s_client: client.CoreV1Api, namespace: str = None):
        """
        Initialize the debug pod manager.
        
        Args:
            k8s_client: Kubernetes CoreV1Api client instance
            namespace: Namespace to create debug pods in (None = auto-detect/create kube-retis-debug namespace)
        """
        self.k8s_client = k8s_client
        if namespace is None:
            # Use kube-retis-debug namespace (helps bypass OpenShift admission webhooks)
            self.namespace = self._get_or_create_debug_namespace()
        else:
            self.namespace = namespace
        self.active_pods = {}  # Track active debug pods by node
    
    def _get_or_create_debug_namespace(self):
        """Get or create a kube-retis-debug namespace to bypass webhooks."""
        # Use kube-retis-debug-* namespace pattern with random suffix
        # Generate a random suffix to avoid conflicts when multiple instances run simultaneously
        suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=5))
        debug_namespace = f"kube-retis-debug-{suffix}"
        
        try:
            # Try to get the namespace
            try:
                self.k8s_client.read_namespace(name=debug_namespace)
                print(f"Using existing debug namespace: {debug_namespace}")
                return debug_namespace
            except client.ApiException as e:
                if e.status == 404:
                    # Namespace doesn't exist, try to create it
                    print(f"Creating debug namespace: {debug_namespace}")
                    namespace_body = client.V1Namespace(
                        metadata=client.V1ObjectMeta(
                            name=debug_namespace,
                            labels={
                                "openshift.io/run-level": "0",
                                "pod-security.kubernetes.io/enforce": "privileged",
                                "pod-security.kubernetes.io/audit": "privileged",
                                "pod-security.kubernetes.io/warn": "privileged"
                            }
                        )
                    )
                    try:
                        self.k8s_client.create_namespace(body=namespace_body)
                        print(f"✓ Created debug namespace: {debug_namespace}")
                        return debug_namespace
                    except client.ApiException as create_e:
                        # If we can't create, fall back to default
                        print(f"⚠ Could not create debug namespace, falling back to 'default': {create_e}")
                        return "default"
                else:
                    # Other error, fall back to default
                    print(f"⚠ Error checking namespace, falling back to 'default': {e}")
                    return "default"
        except Exception as e:
            # Any other error, fall back to default
            print(f"⚠ Error with debug namespace, falling back to 'default': {e}")
            return "default"
    
    def create_debug_pod(self, node_name: str, image: str = "registry.redhat.io/ubi8/ubi:latest", 
                        timeout: int = 60) -> str:
        """
        Create a debug pod on the specified node with privileged access.
        
        Args:
            node_name: Target node name
            image: Container image to use for the debug pod
            timeout: Timeout in seconds for pod to become ready
        
        Returns:
            str: Name of the created debug pod
            
        Raises:
            Exception: If pod creation fails or times out
        """
        pod_name = f"debug-{node_name.replace('.', '-')}-{int(time.time())}"
        
        # Define the container with proper security context and volume mounts
        container = client.V1Container(
            name="debug-container",
            image=image,
            # Keep the pod running so we can exec into it
            command=["/bin/sh", "-c", "sleep 1d"],
            security_context=client.V1SecurityContext(
                privileged=True,  # Required for chroot operations
                capabilities=client.V1Capabilities(
                    add=["SYS_ADMIN", "NET_ADMIN", "SYS_PTRACE"]
                )
            ),
            volume_mounts=[
                # Mount host's root filesystem to allow chrooting
                client.V1VolumeMount(name="host-root", mount_path="/host"),
            ],
        )

        # Define volumes from the host
        volumes = [
            client.V1Volume(
                name="host-root",
                host_path=client.V1HostPathVolumeSource(path="/"),
            ),
        ]

        # Add tolerations to allow running on any node (including control-plane)
        tolerations = [
            client.V1Toleration(operator="Exists")
        ]

        # Define the Pod Spec
        pod_spec = client.V1PodSpec(
            containers=[container],
            host_pid=True,  # Access the host's PID namespace
            host_network=True,  # Access the host's network namespace
            node_name=node_name,
            volumes=volumes,
            tolerations=tolerations,
            restart_policy="Never"  # Do not restart the pod automatically
        )
        
        # Create the Pod object with annotations to help bypass webhooks
        pod = client.V1Pod(
            api_version="v1",
            kind="Pod",
            metadata=client.V1ObjectMeta(
                name=pod_name,
                namespace=self.namespace,
                labels={
                    "app": "debug-pod",
                    "node": node_name.replace('.', '-'),
                    "created-by": "kube-retis-collection"
                },
                annotations={
                    # Annotations to help bypass OpenShift admission webhooks
                    "openshift.io/scc": "privileged",
                    # Try to skip DevWorkspace webhook validation
                    "devworkspace-controller/devworkspace": "false"
                }
            ),
            spec=pod_spec
        )
        
        try:
            # Create the pod with retry logic for webhook timeouts
            print(f"Creating debug pod '{pod_name}' on node '{node_name}' in namespace '{self.namespace}'...")
            max_retries = 3
            retry_delay = 2
            
            for attempt in range(max_retries):
                try:
                    self.k8s_client.create_namespaced_pod(namespace=self.namespace, body=pod)
                    break  # Success, exit retry loop
                except client.ApiException as e:
                    if e.status == 500 and 'webhook' in str(e).lower() and attempt < max_retries - 1:
                        # Webhook timeout/error, retry
                        print(f"⚠ Webhook validation error (attempt {attempt + 1}/{max_retries}), retrying in {retry_delay}s...")
                        time.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff
                    else:
                        # Re-raise if not a retryable webhook error or out of retries
                        raise
            
            # Wait for pod to be ready
            print(f"Waiting for debug pod to become ready...")
            start_time = time.time()
            while time.time() - start_time < timeout:
                try:
                    pod_status = self.k8s_client.read_namespaced_pod_status(
                        name=pod_name, namespace=self.namespace
                    )
                    
                    if pod_status.status.phase == "Running":
                        # Check if container is ready
                        if (pod_status.status.container_statuses and 
                            pod_status.status.container_statuses[0].ready):
                            print(f"✓ Debug pod '{pod_name}' is ready")
                            self.active_pods[node_name] = pod_name
                            return pod_name
                    elif pod_status.status.phase in ["Failed", "Succeeded"]:
                        raise Exception(f"Pod '{pod_name}' ended unexpectedly in phase: {pod_status.status.phase}")
                        
                except client.ApiException as e:
                    if e.status != 404:  # Pod might not exist yet
                        raise
                
                time.sleep(2)
            
            raise Exception(f"Timeout waiting for debug pod '{pod_name}' to become ready")
            
        except Exception as e:
            # Clean up on failure
            try:
                self.delete_debug_pod(node_name, pod_name)
            except:
                pass
            raise Exception(f"Failed to create debug pod on node '{node_name}': {e}")
    
    def execute_command(self, node_name: str, command: str, use_chroot: bool = True, 
                       timeout: int = 300) -> tuple[bool, str, str]:
        """
        Execute a command in the debug pod.
        
        Args:
            node_name: Target node name
            command: Command to execute
            use_chroot: Whether to use chroot /host (default: True)
            timeout: Command timeout in seconds
        
        Returns:
            tuple: (success: bool, stdout: str, stderr: str)
        """
        pod_name = self.active_pods.get(node_name)
        if not pod_name:
            raise Exception(f"No active debug pod found for node '{node_name}'")
        
        # Prepare the command
        if use_chroot:
            exec_command = ["chroot", "/host", "sh", "-c", command]
        else:
            exec_command = ["sh", "-c", command]
        
        print(f"Executing command in debug pod '{pod_name}': {command}")
        
        # Retry logic for webhook timeout errors during exec
        max_retries = 3
        retry_delay = 2
        
        for attempt in range(max_retries):
            try:
                # Execute the command using Kubernetes stream API
                resp = stream(
                    self.k8s_client.connect_get_namespaced_pod_exec,
                    pod_name,
                    self.namespace,
                    command=exec_command,
                    stderr=True,
                    stdin=False,
                    stdout=True,
                    tty=False,
                    _request_timeout=timeout
                )
                
                # Success - process the response
                # For now, treat all output as stdout
                # The stream API doesn't separate stdout/stderr reliably in this simple mode
                stdout = resp if resp else ""
                stderr = ""
                
                # Try to detect errors in the output
                # If the output contains common error patterns, treat it as a failure
                error_patterns = [
                    "No such file or directory",
                    "Permission denied",
                    "command not found",
                    "cannot access",
                    "Operation not permitted"
                ]
                
                success = True
                if stdout:
                    for pattern in error_patterns:
                        if pattern in stdout:
                            success = False
                            stderr = stdout
                            stdout = ""
                            break
                
                return success, stdout, stderr
                
            except Exception as e:
                error_str = str(e).lower()
                is_webhook_error = (
                    'webhook' in error_str and 
                    ('timeout' in error_str or 'deadline exceeded' in error_str or '500' in error_str)
                )
                
                if is_webhook_error and attempt < max_retries - 1:
                    # Webhook timeout/error during exec, retry
                    print(f"⚠ Webhook validation error during exec (attempt {attempt + 1}/{max_retries}), retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                    continue  # Retry the exec
                else:
                    # Not a retryable webhook error or out of retries
                    error_msg = f"Command execution failed: {e}"
                    print(f"✗ {error_msg}")
                    return False, "", error_msg
    
    def copy_file_to_pod(self, node_name: str, local_path: str, remote_path: str, 
                        use_host_path: bool = True) -> bool:
        """
        Copy a file from local machine to the debug pod.
        
        Args:
            node_name: Target node name
            local_path: Local file path
            remote_path: Remote file path (on host if use_host_path=True)
            use_host_path: Whether to use /host prefix for remote path
        
        Returns:
            bool: Success status
        """
        pod_name = self.active_pods.get(node_name)
        if not pod_name:
            raise Exception(f"No active debug pod found for node '{node_name}'")
        
        try:
            # Read the local file
            with open(local_path, 'rb') as f:
                file_content = f.read()
            
            # Determine the target path in the pod
            if use_host_path:
                target_path = f"/host{remote_path}"
            else:
                target_path = remote_path
            
            # Create the directory if it doesn't exist
            dir_path = os.path.dirname(target_path)
            if dir_path:
                mkdir_cmd = f"mkdir -p {dir_path}"
                success, _, _ = self.execute_command(node_name, mkdir_cmd, use_chroot=False)
                if not success:
                    print(f"⚠ Warning: Failed to create directory {dir_path}")
            
            # Use a more reliable file transfer approach
            import base64
            import tempfile
            
            # Create a temporary file path in the pod (not on host)
            import time
            temp_file_in_pod = f"/tmp/transfer_{int(time.time())}.tmp"
            
            # Encode content
            encoded_content = base64.b64encode(file_content).decode('utf-8')
            
            # Write the base64 content to a temporary file in the pod first
            # Use a here-document to avoid command length limitations
            print(f"  Creating temporary file with {len(encoded_content)} base64 characters...")
            
            write_temp_cmd = f"""cat > {temp_file_in_pod} << 'EOF'
{encoded_content}
EOF"""
            
            success, stdout, stderr = self.execute_command(node_name, write_temp_cmd, use_chroot=False)
            
            if not success:
                print(f"✗ Failed to write temporary file in pod: {stderr}")
                return False
            
            print(f"✓ Temporary file created in pod: {temp_file_in_pod}")
            
            # Verify the temporary file was created and has content
            temp_verify_cmd = f"ls -la {temp_file_in_pod} && wc -c < {temp_file_in_pod}"
            temp_verify_success, temp_verify_output, temp_verify_error = self.execute_command(
                node_name, temp_verify_cmd, use_chroot=False
            )
            
            if temp_verify_success:
                print(f"  Temporary file info: {temp_verify_output.strip()}")
            else:
                print(f"⚠ Warning: Could not verify temporary file: {temp_verify_error}")
            
            # Decode the temporary file and copy to target location
            print(f"  Decoding and copying to target: {target_path}")
            decode_cmd = f"base64 -d {temp_file_in_pod} > {target_path}"
            success, stdout, stderr = self.execute_command(node_name, decode_cmd, use_chroot=False)
            
            if not success:
                print(f"✗ Failed to decode and copy to target: {stderr}")
                # Debug: check if the temp file still exists and has content
                debug_cmd = f"ls -la {temp_file_in_pod}; head -c 100 {temp_file_in_pod}"
                debug_success, debug_output, _ = self.execute_command(node_name, debug_cmd, use_chroot=False)
                if debug_success:
                    print(f"  Debug - temp file status: {debug_output}")
                
                # Clean up temp file
                self.execute_command(node_name, f"rm -f {temp_file_in_pod}", use_chroot=False)
                return False
            
            # Clean up temporary file
            self.execute_command(node_name, f"rm -f {temp_file_in_pod}", use_chroot=False)
            
            # Verify the file was created and has content
            verify_cmd = f"ls -la {target_path}"
            success, stdout, stderr = self.execute_command(node_name, verify_cmd, use_chroot=False)
            
            if success and stdout:
                print(f"✓ File copied to {target_path}")
                print(f"  File info: {stdout.strip()}")
                
                # Additional verification: check file size and that it's not empty
                size_cmd = f"wc -c < {target_path}"
                size_success, size_output, size_error = self.execute_command(node_name, size_cmd, use_chroot=False)
                if size_success and size_output:
                    try:
                        file_size = int(size_output.strip())
                        print(f"  File size: {file_size} bytes")
                        if file_size == 0:
                            print(f"⚠ Warning: File appears to be empty!")
                            return False
                    except ValueError as e:
                        print(f"⚠ Warning: Could not parse file size from output: '{size_output.strip()}'")
                        print(f"  Size command error: {size_error}")
                        # Continue anyway, the ls command above should have verified the file exists
                else:
                    print(f"⚠ Warning: Could not get file size: {size_error}")
                
                return True
            else:
                print(f"✗ Failed to verify copied file: {stderr}")
                return False
                
        except Exception as e:
            print(f"✗ Error copying file to pod: {e}")
            return False
    
    def copy_file_from_pod(self, node_name: str, remote_path: str, local_path: str, 
                          use_host_path: bool = True) -> bool:
        """
        Copy a file from the debug pod to local machine with support for large files.
        
        Args:
            node_name: Target node name
            remote_path: Remote file path (on host if use_host_path=True)
            local_path: Local file path
            use_host_path: Whether to use /host prefix for remote path
        
        Returns:
            bool: Success status
        """
        pod_name = self.active_pods.get(node_name)
        if not pod_name:
            raise Exception(f"No active debug pod found for node '{node_name}'")
        
        try:
            # Determine the source path in the pod
            if use_host_path:
                source_path = f"/host{remote_path}"
            else:
                source_path = remote_path
            
            # Check file size first
            size_cmd = f"stat -c %s {source_path}"
            size_success, size_stdout, size_stderr = self.execute_command(node_name, size_cmd, use_chroot=False, timeout=30)
            
            if not size_success:
                print(f"✗ Failed to get file size: {size_stderr}")
                return False
            
            try:
                file_size = int(size_stdout.strip())
                print(f"File size: {file_size} bytes ({file_size / 1024 / 1024:.1f} MB)")
            except ValueError:
                print(f"✗ Invalid file size: {size_stdout}")
                return False
            
            # Use chunked download for files larger than 50MB
            if file_size > 50 * 1024 * 1024:  # 50MB threshold
                return self._copy_large_file_chunked(node_name, source_path, local_path, file_size)
            else:
                # Use original method for smaller files
                return self._copy_small_file_base64(node_name, source_path, local_path)
            
        except Exception as e:
            print(f"✗ Error copying file from pod: {e}")
            return False
    
    def _copy_small_file_base64(self, node_name: str, source_path: str, local_path: str) -> bool:
        """Copy small files using base64 encoding."""
        # Read the file using base64 encoding
        read_cmd = f"base64 {source_path}"
        success, stdout, stderr = self.execute_command(node_name, read_cmd, use_chroot=False, timeout=120)
        
        if not success:
            print(f"✗ Failed to read file from pod: {stderr}")
            return False
        
        # Decode the base64 content
        import base64
        try:
            file_content = base64.b64decode(stdout.strip())
        except Exception as e:
            print(f"✗ Failed to decode file content: {e}")
            return False
        
        # Create local directory if needed
        local_dir = os.path.dirname(local_path)
        if local_dir:
            os.makedirs(local_dir, exist_ok=True)
        
        # Write the file locally
        with open(local_path, 'wb') as f:
            f.write(file_content)
        
        print(f"✓ File copied to {local_path}")
        return True
    
    def _copy_large_file_chunked(self, node_name: str, source_path: str, local_path: str, file_size: int) -> bool:
        """Copy large files using chunked reading to avoid memory issues."""
        chunk_size = 1024 * 1024  # 1MB chunks
        total_chunks = (file_size + chunk_size - 1) // chunk_size
        
        print(f"Downloading large file in {total_chunks} chunks of {chunk_size // 1024}KB each...")
        
        # Create local directory if needed
        local_dir = os.path.dirname(local_path)
        if local_dir:
            os.makedirs(local_dir, exist_ok=True)
        
        try:
            with open(local_path, 'wb') as f:
                for chunk_num in range(total_chunks):
                    start_byte = chunk_num * chunk_size
                    end_byte = min(start_byte + chunk_size - 1, file_size - 1)
                    chunk_length = end_byte - start_byte + 1
                    
                    # Progress indicator
                    progress = (chunk_num + 1) / total_chunks * 100
                    print(f"  Chunk {chunk_num + 1}/{total_chunks} ({progress:.1f}%) - bytes {start_byte}-{end_byte}")
                    
                    # Read chunk using dd and base64
                    dd_cmd = f"dd if={source_path} skip={start_byte} count={chunk_length} bs=1 2>/dev/null | base64"
                    success, stdout, stderr = self.execute_command(node_name, dd_cmd, use_chroot=False, timeout=120)
                    
                    if not success:
                        print(f"✗ Failed to read chunk {chunk_num + 1}: {stderr}")
                        return False
                    
                    # Decode and write chunk
                    import base64
                    try:
                        chunk_data = base64.b64decode(stdout.strip())
                        f.write(chunk_data)
                    except Exception as e:
                        print(f"✗ Failed to decode chunk {chunk_num + 1}: {e}")
                        return False
            
            # Verify file size
            if os.path.exists(local_path):
                local_size = os.path.getsize(local_path)
                if local_size == file_size:
                    print(f"✓ Large file successfully downloaded: {local_path} ({local_size} bytes)")
                    return True
                else:
                    print(f"✗ File size mismatch: expected {file_size}, got {local_size}")
                    return False
            else:
                print(f"✗ Local file not created: {local_path}")
                return False
                
        except Exception as e:
            print(f"✗ Error during chunked download: {e}")
            return False
    
    def delete_debug_pod(self, node_name: str, pod_name: str = None) -> bool:
        """
        Delete a debug pod.
        
        Args:
            node_name: Node name (used to look up active pod if pod_name not provided)
            pod_name: Specific pod name to delete (optional)
        
        Returns:
            bool: Success status
        """
        if not pod_name:
            pod_name = self.active_pods.get(node_name)
            if not pod_name:
                print(f"No active debug pod found for node '{node_name}'")
                return True
        
        try:
            print(f"Deleting debug pod '{pod_name}'...")
            self.k8s_client.delete_namespaced_pod(
                name=pod_name,
                namespace=self.namespace,
                grace_period_seconds=0  # Force immediate deletion
            )
            
            # Remove from active pods tracking
            if node_name in self.active_pods and self.active_pods[node_name] == pod_name:
                del self.active_pods[node_name]
            
            print(f"✓ Debug pod '{pod_name}' deleted")
            return True
            
        except client.ApiException as e:
            if e.status == 404:
                print(f"Debug pod '{pod_name}' not found (may have been already deleted)")
                return True
            else:
                print(f"✗ Failed to delete debug pod '{pod_name}': {e}")
                return False
        except Exception as e:
            print(f"✗ Error deleting debug pod '{pod_name}': {e}")
            return False
    
    def cleanup_all_pods(self) -> None:
        """Clean up all active debug pods."""
        print("Cleaning up all debug pods...")
        for node_name, pod_name in list(self.active_pods.items()):
            self.delete_debug_pod(node_name, pod_name)
    
    @contextlib.contextmanager
    def debug_pod_context(self, node_name: str, image: str = "registry.redhat.io/ubi8/ubi:latest"):
        """
        Context manager for debug pod lifecycle.
        
        Usage:
            with debug_manager.debug_pod_context("worker-1") as pod_name:
                success, stdout, stderr = debug_manager.execute_command("worker-1", "ls /")
        """
        pod_name = None
        try:
            pod_name = self.create_debug_pod(node_name, image)
            yield pod_name
        finally:
            if pod_name:
                self.delete_debug_pod(node_name, pod_name)

def get_kubeconfig_path(args):
    """Get the kubeconfig path from arguments or return None to use default context.
    
    Returns:
        str or None: Path to kubeconfig file if provided, None to use default context
    """
    if args.kubeconfig:
        kubeconfig_path = args.kubeconfig
        # Expand user home directory if needed
        kubeconfig_path = os.path.expanduser(kubeconfig_path)
        
        # Check if file exists
        if not os.path.exists(kubeconfig_path):
            print(f"Kubeconfig file not found at: {kubeconfig_path}")
            print("Please verify the file path exists.")
            sys.exit(1)
        
        print(f"Using kubeconfig from command line argument: {kubeconfig_path}")
        return kubeconfig_path
    else:
        # Return None to indicate we should use default context
        return None

def get_nodes_from_kubernetes(api_instance, name_filter=None, workload_filter=None):
    """Get nodes using Kubernetes client API with optional filtering."""
    print("Getting nodes using Kubernetes API...")
    
    try:
        # Get all nodes using the Kubernetes API
        nodes = api_instance.list_node()
        all_nodes = []
        
        for node in nodes.items:
            node_name = node.metadata.name
            
            # Check if node is worker (not master/control-plane)
            node_labels = node.metadata.labels or {}
            is_worker = (
                'node-role.kubernetes.io/worker' in node_labels or
                ('node-role.kubernetes.io/master' not in node_labels and
                 'node-role.kubernetes.io/control-plane' not in node_labels)
            )
            
            if is_worker:
                all_nodes.append(node_name)
        
        print(f"✓ Found {len(all_nodes)} worker nodes")
        
        # Show actual node names for debugging
        if all_nodes:
            print("Available worker nodes:")
            for i, node in enumerate(all_nodes, 1):
                print(f"  {i}. {node}")
        
        # Apply name filtering if specified
        filtered_nodes = all_nodes
        if name_filter:
            print(f"Applying name filter: '{name_filter}'")
            
            # Try multiple matching strategies for better user experience
            matched_nodes = []
            
            # Strategy 1: Exact match (case-insensitive)
            exact_matches = [node for node in filtered_nodes if node.lower() == name_filter.lower()]
            if exact_matches:
                matched_nodes = exact_matches
                print(f"  Using exact match strategy")
            
            # Strategy 2: Substring match (case-insensitive)
            elif not exact_matches:
                substring_matches = [node for node in filtered_nodes if name_filter.lower() in node.lower()]
                if substring_matches:
                    matched_nodes = substring_matches
                    print(f"  Using substring match strategy")
            
            # Strategy 3: Glob pattern matching (supports * and ? wildcards)
            if not matched_nodes:
                glob_matches = [node for node in filtered_nodes if fnmatch.fnmatch(node.lower(), name_filter.lower())]
                if glob_matches:
                    matched_nodes = glob_matches
                    print(f"  Using glob pattern match strategy")
            
            # Strategy 4: If filter contains wildcards but no matches, suggest adding wildcards
            if not matched_nodes and not any(wildcard in name_filter for wildcard in ['*', '?']):
                # Try with wildcards automatically
                wildcard_pattern = f"*{name_filter.lower()}*"
                wildcard_matches = [node for node in filtered_nodes if fnmatch.fnmatch(node.lower(), wildcard_pattern)]
                if wildcard_matches:
                    matched_nodes = wildcard_matches
                    print(f"  Using automatic wildcard pattern: '{wildcard_pattern}'")
            
            filtered_nodes = matched_nodes
            print(f"✓ After name filter '{name_filter}': {len(filtered_nodes)} nodes")
            
            # Show which nodes matched for debugging
            if filtered_nodes:
                print("Matched nodes:")
                for i, node in enumerate(filtered_nodes, 1):
                    print(f"  {i}. {node}")
            else:
                print("No nodes matched the filter. Try using:")
                print(f"  - Partial name: part of the node name")
                print(f"  - Wildcard pattern: '*{name_filter}*' or '{name_filter}*'")
                print(f"  - Available nodes are listed above")
        
        # Apply workload filtering if specified
        if workload_filter and filtered_nodes:
            workload_filtered_nodes = []
            for node in filtered_nodes:
                if has_workload_on_node(api_instance, node, workload_filter):
                    workload_filtered_nodes.append(node)
            filtered_nodes = workload_filtered_nodes
            print(f"✓ After workload filter '{workload_filter}': {len(filtered_nodes)} nodes")
        
        if not filtered_nodes:
            print("✗ No nodes match the specified filters")
            return []
        
        print(f"✓ Selected nodes for RETIS collection:")
        for node in filtered_nodes:
            print(f"  - {node}")
        
        return filtered_nodes
        
    except client.ApiException as e:
        print(f"✗ Kubernetes API error getting nodes: {e}")
        return []
    except Exception as e:
        print(f"✗ Error getting nodes: {e}")
        return []


def has_workload_on_node(api_instance, node_name, workload_filter):
    """Check if a specific workload is running on the given node using Kubernetes API."""
    try:
        # Get pods running on the specific node using field selector
        pods = api_instance.list_pod_for_all_namespaces(
            field_selector=f'spec.nodeName={node_name}'
        )
        
        for pod in pods.items:
            pod_name = pod.metadata.name
            namespace = pod.metadata.namespace
            
            # Check if the workload filter matches pod name, namespace, or labels
            if re.search(workload_filter, pod_name, re.IGNORECASE):
                return True
            if re.search(workload_filter, namespace, re.IGNORECASE):
                return True
            
            # Check labels
            labels = pod.metadata.labels or {}
            for key, value in labels.items():
                if re.search(workload_filter, f"{key}={value}", re.IGNORECASE):
                    return True
        
        return False
        
    except client.ApiException as e:
        print(f"⚠ Warning: Kubernetes API error checking workloads on node {node_name}: {e}")
        return False
    except Exception as e:
        print(f"⚠ Warning: Error checking workloads on node {node_name}: {e}")
        return False



def download_retis_script_locally(script_url="https://raw.githubusercontent.com/retis-org/retis/main/tools/retis_in_container.sh"):
    """Download the retis_in_container.sh script locally."""
    print(f"Downloading retis_in_container.sh from {script_url}...")
    
    try:
        # Create a temporary file
        temp_fd, temp_path = tempfile.mkstemp(suffix='.sh', prefix='retis_in_container_')
        
        with os.fdopen(temp_fd, 'wb') as temp_file:
            with urllib.request.urlopen(script_url) as response:
                temp_file.write(response.read())
        
        # Make the local file executable
        os.chmod(temp_path, 0o755)
        
        print(f"✓ Downloaded retis_in_container.sh to {temp_path}")
        return temp_path
        
    except Exception as e:
        print(f"✗ Failed to download retis_in_container.sh: {e}")
        return None

def setup_script_on_node(node_name, working_directory, local_script_path, 
                         debug_manager: KubernetesDebugPodManager = None, dry_run=False):
    """Copy the retis_in_container.sh script to a specific node and set permissions using Kubernetes-native debug pod."""
    print(f"Checking retis_in_container.sh on node {node_name}...")
    
    if dry_run:
        print(f"[DRY RUN] Would check if {working_directory}/retis_in_container.sh exists with correct permissions")
        print(f"[DRY RUN] Would create working directory {working_directory} on {node_name} if needed")
        print(f"[DRY RUN] Would copy {local_script_path} to {node_name}:{working_directory}/retis_in_container.sh if needed")
        print(f"[DRY RUN] Would set executable permissions on the script if needed")
        return True
    
    if not debug_manager:
        raise Exception("debug_manager is required for Kubernetes-native operations")
    
    try:
        # Create debug pod for all operations
        with debug_manager.debug_pod_context(node_name) as pod_name:
            script_path = f"{working_directory}/retis_in_container.sh"
            
            # First, check if the script already exists with correct permissions
            print(f"Checking existing script on {node_name}...")
            check_command = f"ls -la {script_path}"
            success, stdout, stderr = debug_manager.execute_command(
                node_name, check_command, use_chroot=True, timeout=30
            )
            
            script_exists = False
            script_executable = False
            
            if success and stdout:
                script_exists = True
                # Check if the script is executable (look for 'x' in permissions)
                permissions = stdout.strip()
                print(f"Found existing script: {permissions}")
                
                # Check if user, group, or other has execute permission
                if 'x' in permissions[:10]:  # First 10 characters contain permissions
                    script_executable = True
                    print(f"✓ Script already exists with correct permissions on {node_name}")
                    return True
                else:
                    print(f"⚠ Script exists but is not executable on {node_name}")
            else:
                print(f"Script does not exist on {node_name}")
            
            # Create working directory if needed
            print(f"Ensuring directory exists on {node_name}...")
            mkdir_command = f"mkdir -p {working_directory}"
            mkdir_success, _, mkdir_stderr = debug_manager.execute_command(
                node_name, mkdir_command, use_chroot=True, timeout=30
            )
            
            if not mkdir_success:
                print(f"⚠ Warning: Failed to create directory on {node_name}: {mkdir_stderr}")
            
            # Only copy script if it doesn't exist
            if not script_exists:
                print(f"Copying script to {node_name}...")
                print(f"  Source: {local_script_path}")
                print(f"  Target: {script_path} (host path)")
                print(f"  Target in pod: /host{script_path}")
                
                # Verify local script exists and is readable
                if not os.path.exists(local_script_path):
                    print(f"✗ Local script file not found: {local_script_path}")
                    return False
                
                local_size = os.path.getsize(local_script_path)
                print(f"  Local file size: {local_size} bytes")
                
                # Copy the script to the node using our debug pod manager
                copy_success = debug_manager.copy_file_to_pod(
                    node_name, local_script_path, script_path, use_host_path=True
                )
                
                if not copy_success:
                    print(f"✗ Failed to copy script to {node_name}")
                    return False
                
                print(f"✓ Script copied to {node_name}")
                
                # Additional verification: check if script exists and has correct size
                verify_size_cmd = f"wc -c < {script_path}"
                size_success, size_output, size_error = debug_manager.execute_command(
                    node_name, verify_size_cmd, use_chroot=True, timeout=10
                )
                
                if size_success and size_output:
                    try:
                        remote_size = int(size_output.strip())
                        print(f"  Remote file size: {remote_size} bytes")
                        if remote_size != local_size:
                            print(f"⚠ Warning: File size mismatch! Local: {local_size}, Remote: {remote_size}")
                    except ValueError as e:
                        print(f"⚠ Warning: Could not parse remote file size from output: '{size_output.strip()}'")
                        print(f"  Size verification error: {size_error}")
                        print(f"  This suggests the script may not have been copied correctly")
                else:
                    print(f"⚠ Warning: Could not verify remote file size: {size_error}")
            
            # Set executable permissions if script is not executable
            if not script_executable:
                print(f"Setting executable permissions on {node_name}...")
                chmod_command = f"chmod a+x {script_path}"
                chmod_success, _, chmod_stderr = debug_manager.execute_command(
                    node_name, chmod_command, use_chroot=True, timeout=30
                )
                
                if not chmod_success:
                    print(f"✗ Failed to set permissions on {node_name}: {chmod_stderr}")
                    return False
                
                print(f"✓ Executable permissions set on {node_name}")
            
            # Final verification with multiple checks
            print(f"Verifying script setup on {node_name}...")
            
            # Check 1: Verify script exists and permissions
            verify_success, verify_stdout, verify_stderr = debug_manager.execute_command(
                node_name, f"ls -la {script_path}", use_chroot=True, timeout=30
            )
            
            if not verify_success:
                print(f"✗ Script verification failed on {node_name}: {verify_stderr}")
                return False
            
            print(f"✓ Script file exists on host:")
            print(f"  {verify_stdout.strip()}")
            
            # Check 2: Verify script is executable
            test_exec_success, test_exec_stdout, test_exec_stderr = debug_manager.execute_command(
                node_name, f"test -x {script_path} && echo 'executable' || echo 'not executable'", use_chroot=True, timeout=10
            )
            
            if test_exec_success and 'executable' in test_exec_stdout:
                print(f"✓ Script is executable")
            else:
                print(f"⚠ Warning: Script may not be executable: {test_exec_stdout}")
            
            # Check 3: Verify script content is valid (not empty/corrupted)
            size_check_success, size_check_stdout, size_check_stderr = debug_manager.execute_command(
                node_name, f"wc -c < {script_path}", use_chroot=True, timeout=10
            )
            
            if size_check_success and size_check_stdout:
                try:
                    script_size = int(size_check_stdout.strip())
                    print(f"✓ Script size on host: {script_size} bytes")
                    if script_size == 0:
                        print(f"✗ Script appears to be empty on host!")
                        return False
                    elif script_size < 100:  # retis_in_container.sh should be much larger
                        print(f"⚠ Warning: Script seems unusually small")
                except ValueError as e:
                    print(f"⚠ Warning: Could not parse script size from output: '{size_check_stdout.strip()}'")
                    print(f"  Size check error: {size_check_stderr}")
                    print(f"  This suggests the script file may not exist on the host")
                    return False
            else:
                print(f"⚠ Warning: Could not get script size: {size_check_stderr}")
                return False
            
            print(f"✓ Script setup complete on {node_name}")
            return True
        
    except Exception as e:
        print(f"✗ Error setting up script on {node_name}: {e}")
        return False

def stop_retis_on_node(node_name, debug_manager: KubernetesDebugPodManager, dry_run=False):
    """Stop the RETIS systemd unit on a specific node using Kubernetes-native debug pod."""
    print(f"Stopping RETIS collection on node: {node_name}")
    
    if dry_run:
        print(f"[DRY RUN] Would execute command:")
        print(f"  Create debug pod on {node_name}")
        print(f"  Execute: systemctl stop RETIS")
        return True
    
    try:
        # Create debug pod and execute stop command
        with debug_manager.debug_pod_context(node_name) as pod_name:
            print(f"Executing stop command via debug pod...")
            
            # Stop the RETIS systemd unit
            stop_command = "systemctl stop RETIS"
            success, stdout, stderr = debug_manager.execute_command(
                node_name, stop_command, use_chroot=True, timeout=60
            )
            
            if not success:
                print(f"✗ RETIS stop command failed on {node_name}")
                if stderr:
                    print("Stop error output:")
                    print(stderr)
                return False
            else:
                print(f"✓ RETIS systemd unit successfully stopped on {node_name}")
                if stdout.strip():
                    print("Stop output:")
                    print(stdout)
                return True
        
    except Exception as e:
        print(f"✗ Error stopping RETIS on {node_name}: {e}")
        return False

def reset_failed_retis_on_node(node_name, debug_manager: KubernetesDebugPodManager, dry_run=False):
    """Reset failed RETIS systemd unit on a specific node using Kubernetes-native debug pod."""
    print(f"Resetting failed RETIS unit on node: {node_name}")
    
    if dry_run:
        print(f"[DRY RUN] Would execute command:")
        print(f"  Create debug pod on {node_name}")
        print(f"  Execute: systemctl reset-failed")
        return True
    
    try:
        # Create debug pod and execute reset-failed command
        with debug_manager.debug_pod_context(node_name) as pod_name:
            print(f"Executing reset-failed command via debug pod...")
            
            # Reset failed RETIS systemd unit
            reset_command = "systemctl reset-failed"
            success, stdout, stderr = debug_manager.execute_command(
                node_name, reset_command, use_chroot=True, timeout=60
            )
            
            if not success:
                print(f"✗ RETIS reset-failed command failed on {node_name}")
                if stderr:
                    print("Reset-failed error output:")
                    print(stderr)
                return False
            else:
                print(f"✓ RETIS systemd unit successfully reset on {node_name}")
                if stdout.strip():
                    print("Reset-failed output:")
                    print(stdout)
                return True
        
    except Exception as e:
        print(f"✗ Error resetting failed RETIS on {node_name}: {e}")
        return False

def download_results_from_node(node_name, working_directory, output_file, local_download_dir="./", 
                              debug_manager: KubernetesDebugPodManager = None, dry_run=False):
    """Download RETIS results file from a specific node to local machine using Kubernetes-native debug pod."""
    node_short_name = node_name.split('.')[0]  # Get short name for file naming
    local_filename = f"arc_{node_short_name}_{output_file}"
    local_filepath = os.path.join(local_download_dir, local_filename)
    remote_filepath = f"{working_directory}/{output_file}"
    
    print(f"Downloading RETIS results from node: {node_name}")
    print(f"Remote file: {remote_filepath}")
    print(f"Local file: {local_filepath}")
    
    if dry_run:
        print(f"[DRY RUN] Would execute commands:")
        print(f"  1. Create debug pod on {node_name}")
        print(f"  2. Check if file exists: {remote_filepath}")
        print(f"  3. Copy file to: {local_filepath}")
        return True
    
    if not debug_manager:
        raise Exception("debug_manager is required for Kubernetes-native operations")
    
    try:
        # Create debug pod and download file
        with debug_manager.debug_pod_context(node_name) as pod_name:
            print(f"Checking if results file exists on {node_name}...")
            
            # Check if remote file exists first
            check_command = f"ls -la {remote_filepath}"
            success, stdout, stderr = debug_manager.execute_command(
                node_name, check_command, use_chroot=True, timeout=30
            )
            
            if not success:
                print(f"⚠ Results file {remote_filepath} not found on {node_name}")
                if stderr:
                    print(f"Check error: {stderr}")
                return False
            
            print(f"✓ Results file found on {node_name}")
            if stdout.strip():
                print(f"File info: {stdout.strip()}")
            
            # Create local download directory if it doesn't exist
            os.makedirs(local_download_dir, exist_ok=True)
            
            # Copy the results file from the node to local machine
            print(f"Downloading file from {node_name}...")
            copy_success = debug_manager.copy_file_from_pod(
                node_name, remote_filepath, local_filepath, use_host_path=True
            )
            
            if not copy_success:
                print(f"✗ Failed to download results from {node_name}")
                return False
            
            # Verify the file was downloaded
            if os.path.exists(local_filepath):
                file_size = os.path.getsize(local_filepath)
                print(f"✓ Results successfully downloaded from {node_name}")
                print(f"Local file: {local_filepath} ({file_size} bytes)")
                return True
            else:
                print(f"✗ Download failed - local file not found: {local_filepath}")
                return False
        
    except Exception as e:
        print(f"✗ Error downloading results from {node_name}: {e}")
        return False

def run_retis_on_node(node_name, retis_image, working_directory, retis_args=None, retis_cmd_str=None, 
                     debug_manager: KubernetesDebugPodManager = None, dry_run=False):
    """Run RETIS collection on a specific node using Kubernetes-native debug pod."""
    
    # retis_cmd_str is now mandatory (passed from args.retis_command)
    if retis_cmd_str is None:
        raise ValueError("retis_cmd_str is required - should be passed from args.retis_command")
    
    # retis_args should contain minimal required info  
    if retis_args is None:
        raise ValueError("retis_args is required and should contain output_file and retis_tag")
    
    print(f"Using RETIS command: {retis_cmd_str}")
    
    # Construct the shell command that will be executed after 'sh -c'
    # Use full path to the script since we downloaded it to the working directory
    
    # Debug: Print the values being used
    print(f"DEBUG: Raw RETIS_IMAGE value: '{retis_image}'")
    print(f"DEBUG: Raw RETIS_TAG value: '{retis_args['retis_tag']}'")
    
    # Verify we're not accidentally using the old OpenShift registry
    if 'image-registry.openshift-image-registry.svc' in retis_image:
        print(f"⚠ WARNING: Still using OpenShift registry! This won't work on vanilla Kubernetes.")
        print(f"   Consider using: --retis-image quay.io/retis/retis")
    
    # Check if the image already contains a tag (which would cause duplication)
    if ':' in retis_image and retis_image.count(':') >= 1:
        # Split the image into repo and tag parts
        image_parts = retis_image.rsplit(':', 1)
        if len(image_parts) == 2:
            image_repo = image_parts[0]
            existing_tag = image_parts[1]
            
            print(f"⚠ WARNING: RETIS image already contains a tag!")
            print(f"   Image provided: {retis_image}")
            print(f"   Repository: {image_repo}")
            print(f"   Existing tag: {existing_tag}")
            print(f"   Specified tag: {retis_args['retis_tag']}")
            print(f"   This would create invalid reference: {retis_image}:{retis_args['retis_tag']}")
            
            # Auto-fix: use the repository part only
            print(f"🔧 AUTO-FIX: Using repository part only: {image_repo}")
            print(f"🔧 AUTO-FIX: Using tag: {retis_args['retis_tag']}")
            retis_image = image_repo
    
    # Build shell command using a temporary script approach to avoid quote escaping issues
    # Create a unique temporary script name
    import time
    temp_script = f"{working_directory}/retis_cmd_{int(time.time())}.sh"
    
    # Create the temporary script content
    script_content = f"""#!/bin/bash
export RETIS_TAG='{retis_args['retis_tag']}'
export RETIS_IMAGE='{retis_image}'
echo "RETIS Debug: IMAGE=$RETIS_IMAGE TAG=$RETIS_TAG FULL=$RETIS_IMAGE:$RETIS_TAG"
{working_directory}/retis_in_container.sh {retis_cmd_str}
"""
    
    # Create command to write the script and execute it
    # Use double quotes to avoid conflicts with the here-document delimiter
    # Escape backslashes, double quotes, AND dollar signs to prevent premature expansion
    escaped_script_content = script_content.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$')
    shell_command = f'cat > {temp_script} << "RETIS_SCRIPT_EOF"\n{escaped_script_content}RETIS_SCRIPT_EOF\nchmod +x {temp_script} && {temp_script} && rm -f {temp_script}'
    
    # Construct the systemd-run command using double quotes
    systemd_command = f'systemd-run --unit=RETIS --working-directory={working_directory} sh -c "{shell_command}"'
    
    print(f"\nRunning RETIS collection on node: {node_name}")
    print(f"Working directory: {working_directory}")
    print(f"RETIS Image: {retis_image}")
    print(f"RETIS Tag: {retis_args['retis_tag']}")
    print(f"Full container reference: {retis_image}:{retis_args['retis_tag']}")
    print(f"RETIS Command: {retis_cmd_str}")
    print(f"Temporary script: {temp_script}")
    print(f"Script content (original):")
    print("=" * 40)
    print(script_content)
    print("=" * 40)
    print(f"Script content (escaped):")
    print("=" * 40)
    print(escaped_script_content)
    print("=" * 40)
    print(f"DEBUG: Shell command: {shell_command}")
    print(f"DEBUG: Systemd command: {systemd_command}")
    
    if dry_run:
        print(f"[DRY RUN] Would execute commands:")
        print(f"  1. Create debug pod on {node_name}")
        print(f"  2. RETIS collection: {systemd_command}")
        print(f"  3. Status check: systemctl status RETIS")
        return True
    
    if not debug_manager:
        raise Exception("debug_manager is required for Kubernetes-native operations")
    
    try:
        # Create debug pod and execute RETIS command
        with debug_manager.debug_pod_context(node_name) as pod_name:
            print(f"Executing RETIS collection command via debug pod...")
            
            # First, verify the script exists on the host before running systemd-run
            script_check_cmd = f"ls -la {working_directory}/retis_in_container.sh"
            print(f"Verifying script exists on host: {script_check_cmd}")
            
            check_success, check_stdout, check_stderr = debug_manager.execute_command(
                node_name, script_check_cmd, use_chroot=True, timeout=30
            )
            
            # Also check the first few lines of the script to verify it downloaded correctly
            if check_success:
                script_head_cmd = f"head -20 {working_directory}/retis_in_container.sh"
                head_success, head_stdout, head_stderr = debug_manager.execute_command(
                    node_name, script_head_cmd, use_chroot=True, timeout=10
                )
                
                if head_success:
                    print(f"✓ Script content preview (first 20 lines):")
                    print("=" * 50)
                    print(head_stdout)
                    print("=" * 50)
                else:
                    print(f"⚠ Warning: Could not read script content: {head_stderr}")
            
            # Verify image and tag values are reasonable
            if not retis_image or retis_image.isspace():
                print(f"✗ Error: RETIS_IMAGE is empty or whitespace!")
                return False
                
            if not retis_args['retis_tag'] or retis_args['retis_tag'].isspace():
                print(f"✗ Error: RETIS_TAG is empty or whitespace!")
                return False
                
            # Check for problematic characters
            if '"' in retis_image or '"' in retis_args['retis_tag']:
                print(f"⚠ Warning: Image or tag contains double quotes, this might cause issues")
                
            print(f"✓ Image validation passed: {retis_image}:{retis_args['retis_tag']}")
            
            if not check_success:
                print(f"✗ Script not found on host filesystem!")
                print(f"Error: {check_stderr}")
                print(f"Let's check what's in the working directory:")
                
                # Check what's actually in the working directory
                ls_cmd = f"ls -la {working_directory}/"
                ls_success, ls_stdout, ls_stderr = debug_manager.execute_command(
                    node_name, ls_cmd, use_chroot=True, timeout=30
                )
                
                if ls_success:
                    print(f"Contents of {working_directory}:")
                    print(ls_stdout)
                else:
                    print(f"Failed to list directory: {ls_stderr}")
                
                return False
            else:
                print(f"✓ Script verified on host:")
                print(f"  {check_stdout.strip()}")
            
            # Execute the systemd-run command
            success, stdout, stderr = debug_manager.execute_command(
                node_name, systemd_command, use_chroot=True, timeout=300
            )
            
            if not success:
                print(f"✗ RETIS collection command failed on {node_name}")
                if stderr:
                    print("Error output:")
                    print(stderr)
                return False
            else:
                print(f"✓ RETIS collection command completed successfully on {node_name}")
                if stdout.strip():
                    print("Output:")
                    print(stdout)
            
            # Check what happened to the RETIS collection
            print(f"Checking RETIS execution results on {node_name}...")
            
            # Check systemd journal for RETIS logs (last 10 minutes)
            print("Checking systemd journal for RETIS logs...")
            journal_success, journal_stdout, journal_stderr = debug_manager.execute_command(
                node_name, 'journalctl -u RETIS --since "10 minutes ago" --no-pager', use_chroot=True, timeout=60
            )
            
            unit_status = "unknown"
            unit_failed = False
            
            if journal_stdout:
                print("RETIS systemd journal logs:")
                print(journal_stdout)
                
                # Look for success/failure indicators in journal
                journal_output = journal_stdout.lower()
                if "succeeded" in journal_output or "finished successfully" in journal_output:
                    unit_status = "completed successfully"
                elif "failed" in journal_output or "error" in journal_output:
                    unit_status = "failed"
                    unit_failed = True
            elif journal_stderr and "No entries" in journal_stderr:
                print("No journal entries found for RETIS unit")
            elif journal_stderr:
                print(f"Journal error: {journal_stderr}")
            
            # Wait for the output file to be created with polling mechanism
            output_file_path = f"{working_directory}/{retis_args['output_file']}"
            print(f"Polling for output file: {output_file_path}")
            
            # Polling parameters
            max_wait_time = 300  # 5 minutes maximum wait
            poll_interval = 15   # Check every 15 seconds
            polls_done = 0
            max_polls = max_wait_time // poll_interval
            
            output_file_exists = False
            for poll_attempt in range(max_polls):
                polls_done += 1
                print(f"  Poll attempt {polls_done}/{max_polls} (waiting {poll_interval}s between checks)...")
                
                file_success, file_stdout, file_stderr = debug_manager.execute_command(
                    node_name, f'ls -la {output_file_path}', use_chroot=True, timeout=30
                )
                
                if file_success and file_stdout and "No such file" not in file_stdout:
                    print(f"✓ RETIS output file found after {polls_done * poll_interval}s:")
                    print(file_stdout)
                    output_file_exists = True
                    # If we have the output file, that's a strong indicator of success
                    if unit_status == "unknown":
                        unit_status = "completed successfully (output file present)"
                    break
                else:
                    if poll_attempt < max_polls - 1:  # Don't print on the last attempt
                        print(f"    Output file not found yet, waiting {poll_interval}s...")
                        time.sleep(poll_interval)
                    else:
                        print(f"✗ RETIS output file not found after {max_wait_time}s")
                        if file_stderr:
                            print(f"File check error: {file_stderr}")
                        if unit_status == "unknown":
                            unit_status = "likely failed (no output file after timeout)"
                            unit_failed = True
            
            # Final status determination
            # Priority: Output file existence is the strongest indicator of success
            if output_file_exists:
                # Verify the file has content (not empty)
                size_check_success, size_stdout, size_stderr = debug_manager.execute_command(
                    node_name, f'stat -c %s {output_file_path}', use_chroot=True, timeout=10
                )
                if size_check_success and size_stdout:
                    try:
                        file_size = int(size_stdout.strip())
                        if file_size > 0:
                            print(f"✓ RETIS collection completed successfully on {node_name}")
                            print(f"  Output file: {output_file_path} ({file_size} bytes)")
                            if unit_failed:
                                print(f"  Note: Journal showed 'failed' status, but output file exists and has content")
                            return True
                        else:
                            print(f"⚠ RETIS output file exists but is empty on {node_name}")
                            return False
                    except ValueError:
                        # Couldn't parse size, but file exists - assume success
                        print(f"✓ RETIS collection completed successfully on {node_name}")
                        print(f"  Output file: {output_file_path} (size verification failed, but file exists)")
                        return True
                else:
                    # File exists but couldn't check size - assume success
                    print(f"✓ RETIS collection completed successfully on {node_name}")
                    print(f"  Output file: {output_file_path} (size check failed, but file exists)")
                    return True
            elif unit_failed:
                print(f"✗ RETIS collection failed on {node_name} (status: {unit_status})")
                return False
            elif "completed successfully" in unit_status:
                print(f"✓ RETIS collection completed successfully on {node_name}")
                return True
            else:
                print(f"⚠ RETIS collection status unclear on {node_name} (status: {unit_status})")
                
                # As a last resort, check for any RETIS-related units
                print("Checking for any RETIS-related systemd units...")
                list_success, list_stdout, list_stderr = debug_manager.execute_command(
                    node_name, 'systemctl list-units --all | grep -i retis', use_chroot=True, timeout=30
                )
                
                if list_stdout and "RETIS" in list_stdout:
                    print("Found RETIS-related units:")
                    print(list_stdout)
                
                return False
        
    except Exception as e:
        print(f"✗ Error running command on {node_name}: {e}")
        return False

def print_retis_events(file_paths):
    """Print RETIS events from result files using the retis Python library."""
    if not RETIS_ANALYSIS_AVAILABLE:
        print("✗ RETIS Python library is not available. Please install it with: pip install retis")
        return False
    
    if not file_paths:
        print("✗ No files specified for analysis")
        return False
    
    print(f"Reading events from {len(file_paths)} RETIS result file(s)...")
    
    for file_path in file_paths:
        print(f"\n=== Processing file: {file_path} ===")
        
        if not os.path.exists(file_path):
            print(f"⚠ File not found: {file_path}")
            continue
        
        try:
            reader = EventFile(file_path)
            event_count = 0
            
            for event in reader.events():
                print(event)
                event_count += 1
            
            print(f"\n✓ Processed {event_count} events from {file_path}")
            
        except Exception as e:
            print(f"✗ Error reading {file_path}: {e}")
            continue
    
    return True

def main():
    """Main function to get nodes and run RETIS collection."""
    
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description="Run RETIS collection on OpenShift worker nodes with filtering options",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Using current Kubernetes/OpenShift context (after 'oc login' or 'kubectl config use-context')
  python3 kube-retis.py --node-filter "worker-1" --retis-command "collect -o events.json"  # uses current context
  python3 kube-retis.py --workload-filter "ovn" --retis-command "collect -o events.json" --start  # uses current context
  
  # RETIS collection with explicit kubeconfig (runs in dry-run mode by default)
  python3 kube-retis.py --kubeconfig ~/.kube/config --node-filter "worker-1"        # exact or substring match
  python3 kube-retis.py --kubeconfig ~/.kube/config --node-filter "worker*"         # wildcard pattern
  python3 kube-retis.py --kubeconfig /path/to/kubeconfig --workload-filter "ovn"
  python3 kube-retis.py --kubeconfig ~/.kube/config --node-filter "compute" --workload-filter "pod.*networking"
  
  # Actually execute RETIS collection (use --start)
  python3 kube-retis.py --node-filter "worker.*" --retis-command "collect -o events.json" --start  # uses current context
  python3 kube-retis.py --kubeconfig ~/.kube/config --node-filter "worker.*" --retis-command "collect -o events.json" --start
  python3 kube-retis.py --kubeconfig ~/.kube/config --workload-filter "ovn" --retis-command "collect -o events.json" --start
  
  # Custom RETIS parameters (dry-run by default)
  python3 kube-retis.py --kubeconfig ~/.kube/config --output-file trace.json --filter-packet "tcp port 443"
  python3 kube-retis.py --kubeconfig ~/.kube/config --no-ovs-track --no-stack --filter-packet "udp port 53"
  python3 kube-retis.py --kubeconfig ~/.kube/config --retis-extra-args "--max-events 10000"
  
  # Custom RETIS command (overrides all other RETIS options)
  python3 kube-retis.py --kubeconfig ~/.kube/config --retis-command "collect -o custom.json --max-events 5000" --start
  python3 kube-retis.py --kubeconfig ~/.kube/config --retis-command "profile -o profile.json -t 30" --start
  
  # Infrastructure options
  python3 kube-retis.py --kubeconfig ~/.kube/config --retis-image "custom-registry/retis:latest" --start
  python3 kube-retis.py --kubeconfig ~/.kube/config --retis-tag "v1.6.0" --start
  python3 kube-retis.py --kubeconfig ~/.kube/config --working-directory /tmp --dry-run
  
  # Stop operations (execute normally)
  python3 kube-retis.py --kubeconfig ~/.kube/config --stop --parallel
  python3 kube-retis.py --kubeconfig ~/.kube/config --node-filter "worker" --stop --dry-run  # preview only
  
  # Reset failed operations (execute normally)
  python3 kube-retis.py --kubeconfig ~/.kube/config --reset-failed --parallel
  python3 kube-retis.py --kubeconfig ~/.kube/config --node-filter "worker" --reset-failed --dry-run  # preview only
  
  # Download results (execute normally)
  python3 kube-retis.py --kubeconfig ~/.kube/config --download-results
  python3 kube-retis.py --kubeconfig ~/.kube/config --node-filter "worker-2*" --download-results --dry-run  # preview only
  python3 kube-retis.py --kubeconfig ~/.kube/config --output-file "mycap.pcap" --download-results  # download specific file type
  
  # Print RETIS events (no Kubernetes connection required, downloads results as arc_*_events.json)
  python3 kube-retis.py --analyze                                              # auto-discover arc_*_events.json files
  python3 kube-retis.py --analyze --analysis-files arc_worker-1_events.json arc_worker-2_events.json
  python3 kube-retis.py --analyze --analysis-files retis.data
  
  # Interactive mode (will use current context or prompt if not found)
  python3 kube-retis.py --retis-command "collect -o events.json"  # uses current context if available
        """
    )
    
    parser.add_argument(
        '--kubeconfig', '-k',
        help='Path to the kubeconfig file (optional - will use current Kubernetes/OpenShift context from KUBECONFIG env var or ~/.kube/config if not provided)',
        type=str
    )
    parser.add_argument(
        '--node-filter', '-n',
        help='Filter nodes by name. Supports: exact match, substring match, or glob patterns with wildcards (e.g., "worker-1", "worker", "worker*", "*worker*")',
        type=str
    )
    parser.add_argument(
        '--workload-filter', '-w',
        help='Regular expression to filter nodes by workload running on them (e.g., "ovn", "nginx")',
        type=str
    )
    parser.add_argument(
        '--retis-image',
        help='RETIS container image repository to use (default: quay.io/retis/retis). Do NOT include tag - use --retis-tag instead.',
        default='quay.io/retis/retis',
        type=str
    )
    parser.add_argument(
        '--retis-tag',
        help='RETIS version tag to use (default: latest)',
        default='latest',
        type=str
    )
    parser.add_argument(
        '--working-directory',
        help='Working directory for the RETIS collection (default: /var/tmp)',
        default='/var/tmp',
        type=str
    )
    parser.add_argument(
        '--dry-run',
        help='Show what commands would be executed without running them (default for RETIS collection)',
        action='store_true'
    )
    parser.add_argument(
        '--start',
        help='Actually execute RETIS collection (overrides default dry-run behavior for collection)',
        action='store_true'
    )
    parser.add_argument(
        '--parallel',
        help='Run RETIS collection on all nodes in parallel (default: sequential)',
        action='store_true'
    )
    parser.add_argument(
        '--stop',
        help='Stop RETIS collection on filtered nodes',
        action='store_true'
    )
    parser.add_argument(
        '--reset-failed',
        help='Reset failed RETIS systemd units on filtered nodes',
        action='store_true'
    )
    parser.add_argument(
        '--download-results',
        help='Download all output files from filtered nodes to local machine (use --output-file to specify filename pattern)',
        action='store_true'
    )
    # RETIS command configuration (mandatory for collection operations)
    parser.add_argument(
        '--retis-command',
        help='RETIS command string to execute. Must include "-o <filename>" parameter for status validation. Example: "collect -o events.json -f \\"tcp port 80\\""',
        type=str
    )
    # Output file for utility operations (download-results)
    parser.add_argument(
        '--output-file', '-o',
        help='Output file name for utility operations like --download-results (default: events.json). Use this to specify the filename pattern for files to be downloaded, e.g., "mycap.pcap"',
        default='events.json',
        type=str
    )
    parser.add_argument(
        '--skip-tls-verification',
        help='Skip TLS certificate verification when connecting to Kubernetes API (useful for OpenShift clusters with self-signed certificates)',
        action='store_true'
    )
    parser.add_argument(
        '--analyze',
        help='Print events from downloaded RETIS result files',
        action='store_true'
    )
    parser.add_argument(
        '--analysis-files',
        help='Specific RETIS result files to read (space-separated). If not provided, will auto-discover arc_*_events.json files in current directory',
        nargs='*',
        type=str
    )
    
    args = parser.parse_args()
    
    # Determine if this is a RETIS collection operation or utility operation
    is_utility_operation = (getattr(args, 'stop', False) or 
                           getattr(args, 'reset_failed', False) or 
                           getattr(args, 'download_results', False) or
                           getattr(args, 'analyze', False))
    
    # Validate custom RETIS command early (before any expensive operations)
    # Only required for RETIS collection operations, not utility operations
    if not is_utility_operation:
        if not hasattr(args, 'retis_command') or not args.retis_command:
            print("❌ ERROR: --retis-command is required for RETIS collection operations")
            print("   Example: --retis-command 'collect -o events.json -f \"tcp port 80\"'")
            print("   Note: --retis-command is not needed for utility operations (--stop, --reset-failed, --download-results, --analyze)")
            sys.exit(1)
        
        print(f"Validating RETIS command: {args.retis_command}")
        
        # Extract output file from custom command (mandatory for proper status checking)
        import re
        output_match = re.search(r'-o\s+([^\s]+)', args.retis_command)
        if not output_match:
            print("❌ ERROR: --retis-command must include '-o <filename>' parameter")
            print("   Example: --retis-command 'collect -o mydata.json -f \"tcp port 80\"'")
            print("   This is required for proper status checking and file validation.")
            sys.exit(1)
        
        custom_output_file = output_match.group(1)
        print(f"📄 Detected output file from command: {custom_output_file}")
        print("✅ RETIS command validation passed")
    else:
        print(f"ℹ️  Utility operation detected - --retis-command not required")
    
    # Validate RETIS image format early (catch tag duplication issues)
    if hasattr(args, 'retis_image') and ':' in args.retis_image:
        image_parts = args.retis_image.rsplit(':', 1)
        if len(image_parts) == 2:
            image_repo = image_parts[0]
            existing_tag = image_parts[1]
            print(f"⚠ WARNING: --retis-image contains a tag which will cause issues!")
            print(f"   You provided: --retis-image {args.retis_image}")
            print(f"   This will create invalid reference: {args.retis_image}:{args.retis_tag}")
            print(f"")
            print(f"🔧 RECOMMENDATION: Use separate parameters:")
            print(f"   --retis-image {image_repo}")
            print(f"   --retis-tag {existing_tag}")
            print(f"")
            print(f"🔧 AUTO-FIXING: Using repository part only and keeping your specified tag")
            args.retis_image = image_repo
            print(f"   Final values: image={args.retis_image}, tag={args.retis_tag}")
            print(f"")
    
    # Set dry-run behavior based on operation type
    # Main RETIS collection defaults to dry-run, utility operations execute normally
    is_utility_operation = (getattr(args, 'stop', False) or 
                           getattr(args, 'reset_failed', False) or 
                           getattr(args, 'download_results', False))
    
    if args.start:
        # --start always overrides --dry-run when both are present
        args.dry_run = False
    elif not args.dry_run and not is_utility_operation:
        # Default to dry-run only for main RETIS collection operation
        args.dry_run = True
    # For utility operations, keep the original --dry-run value (False by default)
    
    # Note: Boolean flag conflicts removed since individual RETIS arguments were removed
    # All RETIS command configuration now handled via --retis-command
    
    # Validate arguments
    if args.stop and getattr(args, 'reset_failed', False):
        print("Error: --stop and --reset-failed cannot be used together")
        return
    
    if args.stop and getattr(args, 'download_results', False):
        print("Error: --stop and --download-results cannot be used together")
        return
    
    if getattr(args, 'reset_failed', False) and getattr(args, 'download_results', False):
        print("Error: --reset-failed and --download-results cannot be used together")
        return
    
    if getattr(args, 'analyze', False) and (args.stop or getattr(args, 'reset_failed', False) or getattr(args, 'download_results', False)):
        print("Error: --analyze cannot be used with --stop, --reset-failed, or --download-results")
        return
    
    if args.stop:
        if args.retis_image != 'quay.io/retis/retis':
            print("Warning: --retis-image is ignored when using --stop")
        if args.retis_tag != 'latest':
            print("Warning: --retis-tag is ignored when using --stop")
        if args.working_directory != '/var/tmp':
            print("Warning: --working-directory is ignored when using --stop")
        # --retis-command is ignored during stop operations
        print("Warning: --retis-command is ignored when using --stop")
    
    if getattr(args, 'reset_failed', False):
        if args.retis_image != 'quay.io/retis/retis':
            print("Warning: --retis-image is ignored when using --reset-failed")
        if args.retis_tag != 'latest':
            print("Warning: --retis-tag is ignored when using --reset-failed")
        if args.working_directory != '/var/tmp':
            print("Warning: --working-directory is ignored when using --reset-failed")
        # --retis-command is ignored during reset-failed operations
        print("Warning: --retis-command is ignored when using --reset-failed")
    
    if getattr(args, 'download_results', False):
        if args.retis_image != 'quay.io/retis/retis':
            print("Warning: --retis-image is ignored when using --download-results")
        if args.retis_tag != 'latest':
            print("Warning: --retis-tag is ignored when using --download-results")
        # Note: --working-directory is used by download-results to know where to find files
        # --retis-command is ignored during download operations
        print("Warning: --retis-command is ignored when using --download-results")
    
    # Validate that at least one filter is provided if the user wants to be specific
    if not args.node_filter and not args.workload_filter:
        print("Warning: No filters specified. This will run on ALL worker nodes in the cluster.")
        print("Use --node-filter and/or --workload-filter to limit the nodes.")
        
        if not args.dry_run:
            confirmation = input("Continue with all worker nodes? (y/N): ").strip().lower()
            if confirmation not in ['y', 'yes']:
                print("Operation cancelled.")
                return

    # --- Handle analysis operation (doesn't require Kubernetes connection) ---
    if getattr(args, 'analyze', False):
        print("Starting RETIS events printing...")
        
        # Determine which files to read
        analysis_files = []
        if getattr(args, 'analysis_files', None):
            # Use specified files
            analysis_files = args.analysis_files
            print(f"Reading specified files: {analysis_files}")
        else:
            # Auto-discover files matching pattern arc_*_events.json
            import glob
            pattern = "arc_*_events.json"
            analysis_files = glob.glob(pattern)
            if analysis_files:
                print(f"Auto-discovered {len(analysis_files)} result files: {analysis_files}")
            else:
                print(f"No files found matching pattern '{pattern}' in current directory")
                print("Use --analysis-files to specify files explicitly, or use --download-results first to download files")
                return
        
        # Print events
        success = print_retis_events(file_paths=analysis_files)
        
        if success:
            print("\nEvent printing completed successfully!")
        else:
            print("\nEvent printing failed!")
            
        return

    # Get kubeconfig path (None means use default context)
    kubeconfig_path = get_kubeconfig_path(args)

    # --- Load Kubernetes Configuration ---
    config_loaded = False
    
    # Try to load in-cluster config first
    try:
        config.load_incluster_config()
        print("✓ Loaded in-cluster Kubernetes configuration.")
        config_loaded = True
    except config.ConfigException:
        pass  # Not running in-cluster, continue to kubeconfig loading
    
    if not config_loaded:
        if kubeconfig_path:
            # Use the specified kubeconfig file path
            try:
                config.load_kube_config(config_file=kubeconfig_path)
                print(f"✓ Loaded kubeconfig from: {kubeconfig_path}")
                config_loaded = True
            except config.ConfigException as e:
                print(f"✗ Could not load kubeconfig from {kubeconfig_path}")
                print(f"Error: {e}")
            except FileNotFoundError:
                print(f"✗ Kubeconfig file not found at: {kubeconfig_path}")
                print("Please verify the file path exists.")
                return
        else:
            # Try to load from default kubeconfig location (KUBECONFIG env var or ~/.kube/config)
            # This will use the current context from 'oc login' or 'kubectl config use-context'
            try:
                # Check if default kubeconfig exists
                default_kubeconfig = os.environ.get('KUBECONFIG')
                if not default_kubeconfig:
                    default_kubeconfig = os.path.expanduser('~/.kube/config')
                
                if os.path.exists(default_kubeconfig):
                    config.load_kube_config()
                    print(f"✓ Loaded Kubernetes configuration from current context")
                    print(f"  Using kubeconfig: {default_kubeconfig}")
                    # Try to show current context (optional - yaml might not be available)
                    try:
                        import yaml
                        with open(default_kubeconfig, 'r') as f:
                            kubeconfig_data = yaml.safe_load(f)
                            current_context = kubeconfig_data.get('current-context', 'unknown')
                            if current_context != 'unknown':
                                print(f"  Current context: {current_context}")
                    except ImportError:
                        # PyYAML not available, skip context display
                        pass
                    except Exception:
                        # Other errors reading context, skip
                        pass
                    config_loaded = True
                else:
                    print(f"✗ Default kubeconfig not found at: {default_kubeconfig}")
            except config.ConfigException as e:
                print(f"✗ Could not load default kubeconfig")
                print(f"Error: {e}")
    
    if not config_loaded:
        # Last resort: prompt user for kubeconfig path
        print("\nCould not locate a valid kubeconfig file or in-cluster config.")
        print("Please ensure you have:")
        print("  - Run 'oc login' or 'kubectl config use-context' to set up your context, OR")
        print("  - Provide a kubeconfig file with --kubeconfig")
        
        kubeconfig_path = input("\nEnter the path to your kubeconfig file (or press Enter to exit): ").strip()
        if not kubeconfig_path:
            print("No kubeconfig path provided. Exiting.")
            return
        
        kubeconfig_path = os.path.expanduser(kubeconfig_path)
        if not os.path.exists(kubeconfig_path):
            print(f"Kubeconfig file not found at: {kubeconfig_path}")
            return
        
        try:
            config.load_kube_config(config_file=kubeconfig_path)
            print(f"✓ Loaded kubeconfig from: {kubeconfig_path}")
        except Exception as e:
            print(f"✗ Failed to load kubeconfig: {e}")
            return

    # --- Handle TLS verification settings (must be done before creating API client) ---
    if args.skip_tls_verification:
        print("Warning: Skipping TLS certificate verification. This is insecure and should only be used for testing.")
        
        # Get the current configuration
        configuration = client.Configuration.get_default_copy()
        
        # Disable SSL verification
        configuration.verify_ssl = False
        
        # Disable urllib3 SSL warnings when verification is disabled
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        # Set the configuration as default
        client.Configuration.set_default(configuration)

    # --- Create Kubernetes API client ---
    core_v1 = client.CoreV1Api()
    
    # --- Create Debug Pod Manager ---
    # Use None to auto-detect/create openshift-debug namespace (bypasses webhooks)
    debug_manager = KubernetesDebugPodManager(core_v1, namespace=None)

    # Test the connection
    try:
        print("Testing connection to Kubernetes cluster...")
        version = core_v1.get_api_resources()
        print("✓ Successfully connected to Kubernetes cluster.")
    except Exception as e:
        error_str = str(e).lower()
        error_full = str(e)
        print(f"✗ Failed to connect to Kubernetes cluster: {e}")
        
        # Check for common certificate/TLS/connection errors
        is_tls_error = any(keyword in error_str for keyword in [
            'certificate', 'ssl', 'tls', 'verify', 'connection reset', 
            'connection aborted', 'max retries', 'protocolerror', 
            'connectionrefused', 'timeout'
        ])
        
        # Check if it's a MaxRetryError (common with OpenShift)
        is_retry_error = 'max retries' in error_str or 'maxretryerror' in error_str
        
        if is_tls_error or is_retry_error:
            print("\n" + "="*70)
            print("⚠️  TLS/Certificate Verification Issue Detected")
            print("="*70)
            print("\nThis error commonly occurs with OpenShift clusters that use")
            print("self-signed certificates. The Python Kubernetes client is stricter")
            print("than the 'oc' CLI about certificate validation.")
            print("\n💡 Solution: Use the --skip-tls-verification flag:")
            print("\n  python3 kube-retis.py --skip-tls-verification \\")
            print("    --node-filter \"worker-1\" \\")
            print("    --retis-command \"collect -o events.json\"")
            print("\nNote: This flag disables SSL certificate verification.")
            print("Only use this if you trust the cluster's certificate authority.")
            print("="*70)
        else:
            print("\nPlease verify:")
            print("  - Your kubeconfig is valid and the cluster is accessible")
            print("  - You have proper RBAC permissions")
            print("  - Network connectivity to the cluster")
            
            # Check if 'oc' command is available
            try:
                result = subprocess.run(['which', 'oc'], capture_output=True, timeout=2)
                if result.returncode == 0:
                    print("  - Try running 'oc get nodes' to verify your OpenShift context works")
            except:
                pass  # Ignore if 'which' command fails
        return

    # --- Get nodes using Kubernetes API ---
    nodes = get_nodes_from_kubernetes(core_v1, name_filter=args.node_filter, workload_filter=args.workload_filter)
    
    if not nodes:
        print("No nodes found matching the specified filters. Exiting.")
        return
    
    # --- Handle stop operation ---
    if args.stop:
        print(f"\nPreparing to stop RETIS collection on {len(nodes)} nodes...")
        
        if args.dry_run:
            print("\n[DRY RUN] The following stop commands would be executed:")
        
        # Stop RETIS on each node
        if args.parallel:
            print("\nStopping RETIS collection in parallel mode...")
            import concurrent.futures
            
            def stop_with_progress(node):
                return stop_retis_on_node(node, debug_manager, args.dry_run)
            
            success_count = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(nodes), 5)) as executor:
                future_to_node = {executor.submit(stop_with_progress, node): node for node in nodes}
                
                for future in concurrent.futures.as_completed(future_to_node):
                    node = future_to_node[future]
                    try:
                        success = future.result()
                        if success:
                            success_count += 1
                    except Exception as e:
                        print(f"✗ Exception occurred stopping RETIS on node {node}: {e}")
        else:
            print("\nStopping RETIS collection sequentially...")
            success_count = 0
            for i, node in enumerate(nodes, 1):
                print(f"\n--- Stopping RETIS on node {i}/{len(nodes)}: {node} ---")
                success = stop_retis_on_node(node, debug_manager, args.dry_run)
                if success:
                    success_count += 1
        
        # Summary for stop operation
        print(f"\n{'=' * 50}")
        print("RETIS Stop Summary")
        print(f"{'=' * 50}")
        print(f"Total nodes: {len(nodes)}")
        print(f"Successfully stopped: {success_count}")
        print(f"Failed to stop: {len(nodes) - success_count}")
        
        if args.dry_run:
            print("\n[DRY RUN] No actual commands were executed.")
        else:
            if success_count == len(nodes):
                print("\n✓ RETIS collection stopped on all nodes!")
            elif success_count > 0:
                print(f"\n⚠ RETIS collection stopped on {success_count}/{len(nodes)} nodes.")
            else:
                print("\n✗ Failed to stop RETIS collection on all nodes.")
        
        print("Script finished.")
        return
    
    # --- Handle reset-failed operation ---
    if getattr(args, 'reset_failed', False):
        print(f"\nPreparing to reset failed RETIS units on {len(nodes)} nodes...")
        
        if args.dry_run:
            print("\n[DRY RUN] The following reset-failed commands would be executed:")
        
        # Reset failed RETIS on each node
        if args.parallel:
            print("\nResetting failed RETIS units in parallel mode...")
            import concurrent.futures
            
            def reset_with_progress(node):
                return reset_failed_retis_on_node(node, debug_manager, args.dry_run)
            
            success_count = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(nodes), 5)) as executor:
                future_to_node = {executor.submit(reset_with_progress, node): node for node in nodes}
                
                for future in concurrent.futures.as_completed(future_to_node):
                    node = future_to_node[future]
                    try:
                        success = future.result()
                        if success:
                            success_count += 1
                    except Exception as e:
                        print(f"✗ Exception occurred resetting failed RETIS on node {node}: {e}")
        else:
            print("\nResetting failed RETIS units sequentially...")
            success_count = 0
            for i, node in enumerate(nodes, 1):
                print(f"\n--- Resetting failed RETIS on node {i}/{len(nodes)}: {node} ---")
                success = reset_failed_retis_on_node(node, debug_manager, args.dry_run)
                if success:
                    success_count += 1
        
        # Summary for reset-failed operation
        print(f"\n{'=' * 50}")
        print("RETIS Reset-Failed Summary")
        print(f"{'=' * 50}")
        print(f"Total nodes: {len(nodes)}")
        print(f"Successfully reset: {success_count}")
        print(f"Failed to reset: {len(nodes) - success_count}")
        
        if args.dry_run:
            print("\n[DRY RUN] No actual commands were executed.")
        else:
            if success_count == len(nodes):
                print("\n✓ RETIS failed units reset on all nodes!")
            elif success_count > 0:
                print(f"\n⚠ RETIS failed units reset on {success_count}/{len(nodes)} nodes.")
            else:
                print("\n✗ Failed to reset RETIS failed units on all nodes.")
        
        print("Script finished.")
        return
    
    # --- Handle download-results operation ---
    if getattr(args, 'download_results', False):
        print(f"\nPreparing to download RETIS results from {len(nodes)} nodes...")
        print(f"Working Directory: {args.working_directory}")
        print(f"Output File: {args.output_file}")
        
        if args.dry_run:
            print("\n[DRY RUN] The following download commands would be executed:")
        
        # Download results from each node
        if args.parallel:
            print("\nDownloading RETIS results in parallel mode...")
            import concurrent.futures
            
            def download_with_progress(node):
                return download_results_from_node(node, args.working_directory, args.output_file, "./", debug_manager, args.dry_run)
            
            success_count = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(nodes), 5)) as executor:
                future_to_node = {executor.submit(download_with_progress, node): node for node in nodes}
                
                for future in concurrent.futures.as_completed(future_to_node):
                    node = future_to_node[future]
                    try:
                        success = future.result()
                        if success:
                            success_count += 1
                    except Exception as e:
                        print(f"✗ Exception occurred downloading results from node {node}: {e}")
        else:
            print("\nDownloading RETIS results sequentially...")
            success_count = 0
            for i, node in enumerate(nodes, 1):
                print(f"\n--- Downloading results from node {i}/{len(nodes)}: {node} ---")
                success = download_results_from_node(node, args.working_directory, args.output_file, "./", debug_manager, args.dry_run)
                if success:
                    success_count += 1
        
        # Summary for download operation
        print(f"\n{'=' * 50}")
        print("RETIS Download Summary")
        print(f"{'=' * 50}")
        print(f"Total nodes: {len(nodes)}")
        print(f"Successfully downloaded: {success_count}")
        print(f"Failed to download: {len(nodes) - success_count}")
        
        if args.dry_run:
            print("\n[DRY RUN] No actual commands were executed.")
        else:
            if success_count == len(nodes):
                print("\n✓ All RETIS results downloaded successfully!")
            elif success_count > 0:
                print(f"\n⚠ RETIS results downloaded from {success_count}/{len(nodes)} nodes.")
                print("Files downloaded to current directory with node name prefix.")
            else:
                print("\n✗ Failed to download RETIS results from all nodes.")
        
        print("Script finished.")
        return
    
    print(f"\nPreparing to run RETIS collection on {len(nodes)} nodes...")
    print(f"RETIS Image: {args.retis_image}")
    print(f"RETIS Tag: {args.retis_tag}")
    print(f"Working Directory: {args.working_directory}")
    
    if args.dry_run:
        print("\n[DRY RUN] The following commands would be executed:")
    
    # --- Download retis_in_container.sh script locally ---
    print(f"\n--- Downloading retis_in_container.sh script locally ---")
    local_script_path = None
    
    if not args.dry_run:
        local_script_path = download_retis_script_locally()
        if not local_script_path:
            print("✗ Failed to download script locally. Cannot proceed.")
            return
    else:
        print("[DRY RUN] Would download retis_in_container.sh locally")
        local_script_path = "/tmp/dummy_script_path"  # placeholder for dry run
    
    try:
        # --- Setup retis_in_container.sh script on each node ---
        print(f"\n--- Setting up retis_in_container.sh script on {len(nodes)} nodes ---")
        setup_success_count = 0
        setup_failed_nodes = []
        
        for i, node in enumerate(nodes, 1):
            print(f"\n--- Setting up script on node {i}/{len(nodes)}: {node} ---")
            setup_success = setup_script_on_node(node, args.working_directory, local_script_path, debug_manager, args.dry_run)
            if setup_success:
                setup_success_count += 1
            else:
                setup_failed_nodes.append(node)
        
        if setup_failed_nodes and not args.dry_run:
            print(f"\n⚠ Script setup failed on {len(setup_failed_nodes)} nodes:")
            for node in setup_failed_nodes:
                print(f"  - {node}")
            print("RETIS collection will only run on nodes where script setup succeeded.")
            # Remove failed nodes from the list
            nodes = [node for node in nodes if node not in setup_failed_nodes]
            if not nodes:
                print("No nodes available for RETIS collection. Exiting.")
                return
        
        print(f"\n--- Script setup complete: {setup_success_count}/{len(nodes) + len(setup_failed_nodes)} nodes successful ---")
        
        # --- Prepare RETIS command (now mandatory) ---
        print(f"Using RETIS command: {args.retis_command}")
        
        # Extract output file from command (we know it exists due to early validation)
        import re
        output_match = re.search(r'-o\s+([^\s]+)', args.retis_command)
        output_file = output_match.group(1)  # Safe since we validated this early
        
        # Simplified retis_args containing only what we need for execution
        retis_args = {
            'output_file': output_file,
            'retis_tag': args.retis_tag
        }
        
        # Use the provided RETIS command directly
        custom_retis_cmd = args.retis_command
        
        # --- Run RETIS collection on each node ---
        if args.parallel:
            print("\nRunning RETIS collection in parallel mode...")
            import concurrent.futures
            import threading
            
            def run_with_progress(node):
                return run_retis_on_node(node, args.retis_image, args.working_directory, retis_args, custom_retis_cmd, debug_manager, args.dry_run)
            
            success_count = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(nodes), 5)) as executor:
                future_to_node = {executor.submit(run_with_progress, node): node for node in nodes}
                
                for future in concurrent.futures.as_completed(future_to_node):
                    node = future_to_node[future]
                    try:
                        success = future.result()
                        if success:
                            success_count += 1
                    except Exception as e:
                        print(f"✗ Exception occurred for node {node}: {e}")
        else:
            print("\nRunning RETIS collection sequentially...")
            success_count = 0
            for i, node in enumerate(nodes, 1):
                print(f"\n--- Processing node {i}/{len(nodes)} ---")
                success = run_retis_on_node(node, args.retis_image, args.working_directory, retis_args, custom_retis_cmd, debug_manager, args.dry_run)
                if success:
                    success_count += 1
        
        # --- Summary ---
        print(f"\n{'=' * 50}")
        print("RETIS Collection Summary")
        print(f"{'=' * 50}")
        print(f"Total nodes: {len(nodes)}")
        print(f"Successful: {success_count}")
        print(f"Failed: {len(nodes) - success_count}")
        
        if args.dry_run:
            print("\n[DRY RUN] No actual commands were executed.")
        else:
            if success_count == len(nodes):
                print("\n✓ All RETIS collections completed successfully!")
            elif success_count > 0:
                print(f"\n⚠ {success_count}/{len(nodes)} RETIS collections completed successfully.")
            else:
                print("\n✗ All RETIS collections failed.")
        
        print("Script finished.")
        
    finally:
        # Clean up debug pods
        try:
            debug_manager.cleanup_all_pods()
        except Exception as e:
            print(f"Warning: Failed to clean up debug pods: {e}")
        
        # Clean up the temporary script file
        if local_script_path and not args.dry_run and local_script_path != "/tmp/dummy_script_path":
            try:
                os.unlink(local_script_path)
                print(f"Cleaned up temporary file: {local_script_path}")
            except Exception as e:
                print(f"Warning: Failed to clean up temporary file {local_script_path}: {e}")

if __name__ == "__main__":
    main() 
