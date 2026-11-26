"""Structured SSH chains: exact host keys, private credentials, fixed probe code."""

import io
import json
import shlex
import socket
import time
from contextlib import ExitStack, contextmanager

import paramiko

from .resource_probe import probe_machine, probe_program
from .resource_registry import ResourceVault, fingerprint


class ConnectionFailure(Exception):
    def __init__(self, result):
        self.result = result
        super().__init__(result["message"])


class ResourceTransport:
    def __init__(self, root):
        self.vault = ResourceVault(root)

    @contextmanager
    def connect(self, server):
        connection = server["connection"]
        if connection["transport"] == "manual":
            raise ConnectionFailure(
                {
                    "ready": False,
                    "stage": "manual_prerequisite",
                    "message": "需要人工接入及获准执行通道；未执行网页登录或系统安装。",
                }
            )
        endpoints = [*connection["jumps"], connection["target"]]
        with ExitStack() as stack:
            parent = None
            for index, endpoint in enumerate(endpoints):
                try:
                    channel = (
                        parent.open_channel(
                            "direct-tcpip",
                            (endpoint["host"], endpoint["port"]),
                            ("127.0.0.1", 0),
                            timeout=10,
                        )
                        if parent
                        else socket.create_connection(
                            (endpoint["host"], endpoint["port"]), timeout=10
                        )
                    )
                    stack.callback(channel.close)
                    transport = paramiko.Transport(channel)
                    stack.callback(transport.close)
                    transport.start_client(timeout=10)
                    current = fingerprint(transport.get_remote_server_key())
                    if current != endpoint.get("fingerprint"):
                        raise ConnectionFailure(
                            {
                                "ready": False,
                                "stage": "host_identity",
                                "hop": index,
                                "fingerprint": current,
                                "mismatch": bool(endpoint.get("fingerprint")),
                                "message": "主机指纹未核对或已变化；未提交该跳凭据。请向管理员核对后由所有者确认。",
                            }
                        )
                    if endpoint["auth"] == "password":
                        transport.auth_password(
                            endpoint["username"],
                            self.vault.get(endpoint["credential_ref"], "password"),
                        )
                    elif endpoint["auth"] == "private_key":
                        material = self.vault.get(endpoint["credential_ref"], "private_key")
                        key = None
                        for kind in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
                            try:
                                key = kind.from_private_key(io.StringIO(material))
                                break
                            except (paramiko.SSHException, ValueError):
                                continue
                        if key is None:
                            raise ValueError(
                                "Private key format unavailable; encrypted keys need an admitted SSH agent"
                            )
                        transport.auth_publickey(endpoint["username"], key)
                    else:
                        agent = paramiko.Agent()
                        stack.callback(agent.close)
                        for key in agent.get_keys():
                            try:
                                transport.auth_publickey(endpoint["username"], key)
                                break
                            except paramiko.AuthenticationException:
                                continue
                    if not transport.is_authenticated():
                        raise paramiko.AuthenticationException(
                            "No admitted authentication succeeded"
                        )
                    parent = transport
                except ConnectionFailure:
                    raise
                except (OSError, paramiko.SSHException, ValueError) as error:
                    stage = (
                        "authentication"
                        if isinstance(error, (paramiko.AuthenticationException, ValueError))
                        else "transport"
                    )
                    # Exception text can contain credential or remote details. Return categories only.
                    raise ConnectionFailure(
                        {
                            "ready": False,
                            "stage": stage,
                            "hop": index,
                            "message": "该访问阶段未成功：" + type(error).__name__,
                        }
                    ) from None
            yield parent

    def observe(self, server):
        if server["connection"]["transport"] == "local":
            return probe_machine()
        with self.connect(server) as transport:
            channel = transport.open_session(timeout=10)
            try:
                channel.exec_command("python3 -c " + shlex.quote(probe_program()))
                output = bytearray()
                deadline = time.monotonic() + 100
                while time.monotonic() < deadline:
                    if channel.recv_ready():
                        output.extend(channel.recv(65536))
                        if len(output) > 2000000:
                            raise ConnectionFailure(
                                {"ready": False, "stage": "probe", "message": "观测超过输出预算"}
                            )
                    if channel.recv_stderr_ready():
                        channel.recv_stderr(65536)
                    # An exit status can arrive before the final stdout/EOF is
                    # visible to this thread. Drain the complete SSH stream.
                    if (
                        (channel.eof_received or channel.closed)
                        and channel.exit_status_ready()
                        and not channel.recv_ready()
                    ):
                        break
                    time.sleep(0.02)
                else:
                    raise ConnectionFailure(
                        {"ready": False, "stage": "probe", "message": "只读观测超时"}
                    )
                if channel.recv_exit_status() != 0:
                    raise ConnectionFailure(
                        {
                            "ready": False,
                            "stage": "probe",
                            "message": "目标需要可用Python3以执行固定只读观测",
                        }
                    )
                try:
                    result = json.loads(output)
                    if result["scope"] != "whole-machine" or result["schema_version"] != 1:
                        raise ValueError()
                except (ValueError, KeyError, TypeError):
                    raise ConnectionFailure(
                        {"ready": False, "stage": "probe", "message": "目标返回的观测格式无法验证"}
                    ) from None
                return result
            finally:
                channel.close()

    def test(self, server):
        try:
            if server["connection"]["transport"] == "local":
                return {
                    "ready": True,
                    "stage": "local",
                    "origin": "pilot_host",
                    "observed_at": time.time(),
                    "capabilities": ["observe", "registered_local_jobs"],
                }
            with self.connect(server):
                pass
            return {
                "ready": True,
                "stage": "authenticated",
                "origin": "pilot_host",
                "observed_at": time.time(),
                "capabilities": ["fixed_readonly_probe"],
                "serving_executor": "not_registered",
            }
        except ConnectionFailure as error:
            return {**error.result, "origin": "pilot_host", "observed_at": time.time()}
