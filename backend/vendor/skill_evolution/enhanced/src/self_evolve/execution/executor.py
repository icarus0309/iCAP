"""Portable executor abstraction for running Hermes across different environments."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class ExecutorConfig:
    """Configuration for different execution environments."""

    # Executor type: "local", "wsl", "docker", "ssh"
    executor_type: str = "local"

    # For WSL: WSL distribution name (e.g., "Ubuntu")
    wsl_distro: Optional[str] = None

    # For Docker: image name and optional container options
    docker_image: Optional[str] = None
    docker_options: Optional[List[str]] = None

    # For SSH: host, user, port
    ssh_host: Optional[str] = None
    ssh_user: Optional[str] = None
    ssh_port: int = 22

    # Working directory in the target environment
    work_dir: Optional[str] = None

    # Environment variables to pass
    env_vars: Dict[str, str] = None

    def __post_init__(self):
        if self.env_vars is None:
            self.env_vars = {}
        if self.docker_options is None:
            self.docker_options = []


class Executor:
    """Base executor interface."""

    def run(
        self,
        command: List[str],
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> subprocess.CompletedProcess:
        """Run a command in the target environment."""
        raise NotImplementedError


class LocalExecutor(Executor):
    """Execute commands locally."""

    def __init__(self, config: ExecutorConfig):
        self.config = config

    def run(
        self,
        command: List[str],
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> subprocess.CompletedProcess:
        """Run command locally."""
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
        full_env.update(self.config.env_vars)

        return subprocess.run(
            command,
            cwd=cwd,
            env=full_env,
            timeout=timeout,
            text=True,
            capture_output=True,
            check=False,
        )

class WSLExecutor(Executor):
      """Execute commands in WSL."""

      def __init__(self, config: ExecutorConfig):
          self.config = config
          self.distro = config.wsl_distro or "Ubuntu"

      @staticmethod
      def _windows_to_wsl_path(path: Path) -> str:
          """Convert Windows path to WSL path."""
          path_str = str(path).replace("\\", "/")
          if len(path_str) >= 2 and path_str[1] == ":":
              drive = path_str[0].lower()
              rest = path_str[2:]
              return f"/mnt/{drive}{rest}"
          return path_str

      def run(
          self,
          command: List[str],
          cwd: Optional[Path] = None,
          env: Optional[Dict[str, str]] = None,
          timeout: Optional[int] = None,
      ) -> subprocess.CompletedProcess:
          # Convert working directory to WSL path
          wsl_cwd = None
          if cwd:
              wsl_cwd = self._windows_to_wsl_path(cwd)
          elif self.config.work_dir:
              wsl_cwd = self.config.work_dir

          # Build WSL command
          wsl_cmd = ["wsl", "-d", self.distro]
          if wsl_cwd:
              wsl_cmd.extend(["--cd", wsl_cwd])

          # Convert environment variables: Windows paths to WSL paths
          wsl_env = {}
          if env:
              for key, value in env.items():
                  # Convert Windows paths to WSL paths for specific variables
                  if key in ("HERMES_HOME", "PYTHONPATH") and isinstance(value, str):
                      # Handle multiple paths separated by os.pathsep
                      if os.pathsep in value:
                          paths = value.split(os.pathsep)
                          wsl_paths = [self._windows_to_wsl_path(Path(p)) for p in paths]
                          wsl_env[key] = ":".join(wsl_paths)
                      else:
                          wsl_env[key] = self._windows_to_wsl_path(Path(value))
                  else:
                      wsl_env[key] = value

          # Add config env vars
          wsl_env.update(self.config.env_vars)

          # Compose environment setup string
          env_setup = " ".join(f"{k}={v}" for k, v in wsl_env.items()) if wsl_env else ""

          # Compose command string with environment variables
          cmd_str = " ".join(f'"{arg}"' if " " in arg else arg for arg in command)
          if env_setup:
              cmd_str = f"{env_setup} {cmd_str}"

          wsl_cmd.extend(["bash", "-l", "-c", cmd_str])

          # Run WSL command
          return subprocess.run(
              wsl_cmd,
              timeout=timeout,
              encoding="utf-8",
              capture_output=True,
              check=False,
          )

class DockerExecutor(Executor):
    """Execute commands in Docker."""

    def __init__(self, config: ExecutorConfig):
        self.config = config
        if not config.docker_image:
            raise ValueError("docker_image must be specified for DockerExecutor")

    def run(
        self,
        command: List[str],
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> subprocess.CompletedProcess:
        """Run command in Docker."""
        docker_cmd = ["docker", "run", "--rm"]

        # Add options
        if self.config.docker_options:
            docker_cmd.extend(self.config.docker_options)

        # Add working directory
        if cwd or self.config.work_dir:
            work_dir = self.config.work_dir or str(cwd)
            docker_cmd.extend(["-w", work_dir])

        # Add environment variables
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
        full_env.update(self.config.env_vars)

        for key, value in full_env.items():
            docker_cmd.extend(["-e", f"{key}={value}"])

        # Add image and command
        docker_cmd.append(self.config.docker_image)
        docker_cmd.extend(command)

        return subprocess.run(
            docker_cmd,
            timeout=timeout,
            capture_output=False,
        )


class SSHExecutor(Executor):
    """Execute commands via SSH."""

    def __init__(self, config: ExecutorConfig):
        self.config = config
        if not config.ssh_host:
            raise ValueError("ssh_host must be specified for SSHExecutor")

    def run(
        self,
        command: List[str],
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
    ) -> subprocess.CompletedProcess:
        """Run command via SSH."""
        ssh_cmd = ["ssh"]

        # Add port if not default
        if self.config.ssh_port != 22:
            ssh_cmd.extend(["-p", str(self.config.ssh_port)])

        # Add user and host
        host = self.config.ssh_host
        if self.config.ssh_user:
            host = f"{self.config.ssh_user}@{host}"
        ssh_cmd.append(host)

        # Build remote command
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
        full_env.update(self.config.env_vars)

        # Build environment setup
        env_setup = " ".join(f"{k}={v}" for k, v in full_env.items())

        # Build working directory change
        cd_cmd = ""
        if cwd or self.config.work_dir:
            work_dir = self.config.work_dir or str(cwd)
            cd_cmd = f"cd {work_dir} && "

        # Build full remote command
        cmd_str = " ".join(f'"{arg}"' if " " in arg else arg for arg in command)
        remote_cmd = f"{env_setup} {cd_cmd}{cmd_str}"

        ssh_cmd.append(remote_cmd)

        return subprocess.run(
            ssh_cmd,
            timeout=timeout,
            capture_output=False,
        )


def create_executor(config: ExecutorConfig) -> Executor:
    """Factory function to create appropriate executor."""
    executor_type = config.executor_type.lower()

    if executor_type == "local":
        return LocalExecutor(config)
    elif executor_type == "wsl":
        return WSLExecutor(config)
    elif executor_type == "docker":
        return DockerExecutor(config)
    elif executor_type == "ssh":
        return SSHExecutor(config)
    else:
        raise ValueError(f"Unknown executor type: {executor_type}")


def load_executor_config_from_env() -> ExecutorConfig:
    """Load executor configuration from environment variables."""
    executor_type = os.getenv("EXECUTOR_TYPE", "local")

    config = ExecutorConfig(executor_type=executor_type)

    if executor_type == "wsl":
        config.wsl_distro = os.getenv("WSL_DISTRO", "Ubuntu")
    elif executor_type == "docker":
        config.docker_image = os.getenv("DOCKER_IMAGE")
        docker_opts = os.getenv("DOCKER_OPTIONS", "")
        if docker_opts:
            config.docker_options = docker_opts.split()
    elif executor_type == "ssh":
        config.ssh_host = os.getenv("SSH_HOST")
        config.ssh_user = os.getenv("SSH_USER")
        config.ssh_port = int(os.getenv("SSH_PORT", "22"))

    config.work_dir = os.getenv("EXECUTOR_WORK_DIR")

    return config
