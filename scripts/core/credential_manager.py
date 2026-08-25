# -*- coding: utf-8 -*-
"""Credential Manager - T1 直连层凭证安全存储。

凭证不进 task_flow.json，用 credential_ref 引用。支持三种后端：
  1) keyring  - Windows Credential Manager（推荐，OS 级加密）
  2) env      - 环境变量（适合 CI/CD）
  3) file     - 加密 JSON 文件（AES-Fernet，适合无 keyring 环境）

用法：
    mgr = CredentialManager()
    mgr.store("kingdee_token", {"token": "abc123"}, backend="keyring")
    val = mgr.get("kingdee_token")       # -> {"token": "abc123"}
    mgr.delete("kingdee_token")

task_flow.json 里只写 credential_ref：
    {"type": "api", "func": "call_api", "args": ["query_orders"],
     "credential_ref": "kingdee_token"}
"""
import os
import json
import base64

# keyring 可选依赖
try:
    import keyring as _keyring
    _HAS_KEYRING = True
except ImportError:
    _HAS_KEYRING = False

# cryptography 可选依赖（file 后端用）
try:
    from cryptography.fernet import Fernet
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False


_SERVICE_NAME = "autopilot-composer"


class CredentialManager:
    """统一凭证存取，自动选择最优后端。"""

    def __init__(self, file_path=None):
        self.file_path = file_path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            ".credentials.json.enc"
        )

    def store(self, ref, value, backend="auto"):
        """存储凭证。backend: auto/keyring/env/file。"""
        backend = self._resolve_backend(backend)
        if backend == "keyring":
            _keyring.set_password(_SERVICE_NAME, ref, json.dumps(value))
        elif backend == "env":
            # 环境变量只存字符串；复杂结构用 JSON 编码
            os.environ["APC_CRED_" + ref.upper()] = json.dumps(value)
        elif backend == "file":
            self._store_file(ref, value)
        return backend

    def get(self, ref, backend="auto"):
        """读取凭证。返回 dict 或 None。"""
        backend = self._resolve_backend(backend)
        if backend == "keyring":
            raw = _keyring.get_password(_SERVICE_NAME, ref)
            return json.loads(raw) if raw else None
        elif backend == "env":
            raw = os.environ.get("APC_CRED_" + ref.upper())
            return json.loads(raw) if raw else None
        elif backend == "file":
            return self._load_file(ref)
        return None

    def delete(self, ref, backend="auto"):
        backend = self._resolve_backend(backend)
        if backend == "keyring":
            try:
                _keyring.delete_password(_SERVICE_NAME, ref)
            except Exception:
                pass
        elif backend == "env":
            os.environ.pop("APC_CRED_" + ref.upper(), None)
        elif backend == "file":
            data = self._read_file_all()
            data.pop(ref, None)
            self._write_file_all(data)

    def list_refs(self, backend="auto"):
        """列出所有已存储的 credential_ref。"""
        backend = self._resolve_backend(backend)
        if backend == "file":
            return sorted(self._read_file_all().keys())
        # keyring/env 无法枚举，返回空
        return []

    # ---- 内部 ----

    def _resolve_backend(self, backend):
        if backend != "auto":
            return backend
        if _HAS_KEYRING:
            return "keyring"
        return "file"

    def _store_file(self, ref, value):
        data = self._read_file_all()
        data[ref] = value
        self._write_file_all(data)

    def _load_file(self, ref):
        data = self._read_file_all()
        return data.get(ref)

    def _read_file_all(self):
        if not os.path.exists(self.file_path):
            return {}
        try:
            with open(self.file_path, "rb") as f:
                raw = f.read()
            if not raw:
                return {}
            key = self._get_key()
            decrypted = Fernet(key).decrypt(raw)
            return json.loads(decrypted)
        except Exception:
            return {}

    def _write_file_all(self, data):
        key = self._get_key()
        encrypted = Fernet(key).encrypt(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        with open(self.file_path, "wb") as f:
            f.write(encrypted)

    def _get_key(self):
        """从固定路径读或生成加密密钥。"""
        key_path = self.file_path + ".key"
        if os.path.exists(key_path):
            with open(key_path, "rb") as f:
                return f.read()
        key = Fernet.generate_key()
        with open(key_path, "wb") as f:
            f.write(key)
        return key


# 全局单例
_default = None


def get_manager():
    global _default
    if _default is None:
        _default = CredentialManager()
    return _default


def resolve_credential(credential_ref):
    """便捷函数：从 task_flow.json 的 credential_ref 解析出凭证 dict。"""
    if not credential_ref:
        return {}
    mgr = get_manager()
    cred = mgr.get(credential_ref)
    if cred is None:
        # 最后兜底：尝试当作明文环境变量名
        env_val = os.environ.get(credential_ref)
        if env_val:
            return {"value": env_val}
    return cred or {}
