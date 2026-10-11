"""M7A profile 加解密工具（与 pack_profile.ps1 格式严格一致）。

文件格式（大端）：
  MAGIC  8 字节  "M7APROF1"
  VER    1 字节   0x01
  ITER   4 字节   PBKDF2 迭代次数（大端 int32）
  SALT  16 字节
  NONCE 12 字节
  TAG   16 字节
  CIPHER 剩余

用法：
  export M7A_PROFILE_KEY='...'
  python m7a_profile_crypto.py decrypt <in.enc> <out.tar.gz>
  python m7a_profile_crypto.py encrypt <in.tar.gz> <out.enc>
"""
import os
import struct
import sys

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

MAGIC = b"M7APROF1"
VERSION = 1
DEFAULT_ITERATIONS = 300000
SALT_LEN = 16
NONCE_LEN = 12
TAG_LEN = 16
HEADER_LEN = 8 + 1 + 4 + SALT_LEN + NONCE_LEN + TAG_LEN


def derive_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    return PBKDF2HMAC(algorithm=SHA256(), length=32, salt=salt,
                      iterations=iterations).derive(passphrase.encode("utf-8"))


def do_encrypt(src: str, dst: str, passphrase: str,
               iterations: int = DEFAULT_ITERATIONS) -> int:
    with open(src, "rb") as f:
        plain = f.read()

    salt = os.urandom(SALT_LEN)
    nonce = os.urandom(NONCE_LEN)
    key = derive_key(passphrase, salt, iterations)

    blob = AESGCM(key).encrypt(nonce, plain, None)
    cipher, tag = blob[:-TAG_LEN], blob[-TAG_LEN:]

    with open(dst, "wb") as f:
        f.write(MAGIC)
        f.write(bytes([VERSION]))
        f.write(struct.pack(">i", iterations))
        f.write(salt)
        f.write(nonce)
        f.write(tag)
        f.write(cipher)

    print(f"加密完成: {dst}  明文 {len(plain)} 字节 -> 密文 {os.path.getsize(dst)} 字节")
    return 0


def do_decrypt(src: str, dst: str, passphrase: str) -> int:
    with open(src, "rb") as f:
        blob = f.read()

    if len(blob) < HEADER_LEN:
        print(f"错误: 文件过短（{len(blob)} 字节），不是有效格式")
        return 2
    if blob[:8] != MAGIC:
        print(f"错误: 文件头不匹配，期望 {MAGIC!r}，实际 {blob[:8]!r}")
        return 2

    version = blob[8]
    (iterations,) = struct.unpack(">i", blob[9:13])
    salt = blob[13:29]
    nonce = blob[29:41]
    tag = blob[41:57]
    cipher = blob[57:]

    print(f"格式版本 {version}，PBKDF2 迭代 {iterations}，"
          f"密文 {len(cipher)} 字节")

    key = derive_key(passphrase, salt, iterations)
    try:
        plain = AESGCM(key).decrypt(nonce, cipher + tag, None)
    except Exception as e:
        print(f"解密失败（口令错误或文件损坏）: {type(e).__name__}")
        return 1

    with open(dst, "wb") as f:
        f.write(plain)
    print(f"解密完成: {dst}  ({len(plain)} 字节)")
    return 0


def main():
    if len(sys.argv) != 4 or sys.argv[1] not in ("encrypt", "decrypt"):
        print(__doc__)
        return 2

    mode, src, dst = sys.argv[1], sys.argv[2], sys.argv[3]
    passphrase = os.environ.get("M7A_PROFILE_KEY", "")
    if not passphrase:
        print("错误: 环境变量 M7A_PROFILE_KEY 为空")
        return 2

    if not os.path.exists(src):
        print(f"错误: 找不到输入文件 {src}")
        return 2

    if mode == "encrypt":
        return do_encrypt(src, dst, passphrase)
    return do_decrypt(src, dst, passphrase)


if __name__ == "__main__":
    sys.exit(main())
