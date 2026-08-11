"""
X-Bogus 纯 Python 实现 (移植自 ylcangel/douyin_sign/x_bogus/xbogus.py, Apache-2.0)

算法: URI-MD5 + salt-MD5 + RC4 + 自定义base64(s1字母表)
无需 V8/webmssdk，仅依赖标准库。
"""

import hashlib
import random


def rc4_bytes(key: bytes, data: bytes) -> bytes:
    """自实现 RC4 (与 JS charCodeAt 字节值一致)"""
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) % 256
        s[i], s[j] = s[j], s[i]
    i = j = 0
    result = bytearray()
    for byte in data:
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        k = s[(s[i] + s[j]) % 256]
        result.append(byte ^ k)
    return bytes(result)


qlist = {
    'bogusIndex': 0,
    'envcode': 0,
}

ucode = {'ubcode': 0}


def action_valid():
    bits = [random.getrandbits(1) for _ in range(8)]
    ucode['ubcode'] = bits[0] | bits[1] | bits[2] | bits[3] | bits[4] | bits[5] | bits[6] | bits[7]
    ucode['ubcode'] &= ~126
    qlist['envcode'] = 1


def xbogus_fun(protocol, revent_flag, arg3, in_salt, data):
    """核心算法。data 为 URI 字符串 (内部取 MD5)。"""
    action_valid()
    in_salt = in_salt or ''
    in_salt_md5 = hashlib.md5(in_salt.encode('utf-8')).digest()
    if data is None:
        data = '00000000000000000000000000000000'

    uarray = bytearray(9)
    pad_string = (protocol << 6) | (revent_flag << 5) | ((int(random.random() * 100) & 1) << 4) | 0

    qlist['bogusIndex'] += 1
    bogus_index = qlist['bogusIndex'] & 0x3f
    uarray[0] = (arg3 << 6) | bogus_index
    uarray[1] = (qlist['envcode'] >> 8) & 255
    uarray[2] = qlist['envcode'] & 255
    uarray[3] = ucode['ubcode']
    in_salt_md5_md5 = hashlib.md5(in_salt_md5).digest()
    uarray[4] = in_salt_md5_md5[14]
    uarray[5] = in_salt_md5_md5[15]
    data_md5 = hashlib.md5(data.encode('utf-8') if isinstance(data, str) else data).digest()
    uarray[6] = data_md5[14]
    uarray[7] = data_md5[15]
    uarray[8] = int(random.random() * 255) & 255
    return xcrypto(pad_string, uarray)


def xcrypto(pad_string, ostring):
    length = len(ostring)
    uarray = bytearray(length + 1)
    ieor = 0
    for i in range(length):
        uarray[i] = ostring[i]
        ieor ^= ostring[i]
    uarray[length] = ieor
    random_key = random.randint(0, 255)
    crypted = rc4_bytes(bytes([random_key]), bytes(uarray))
    result = chr(pad_string) + chr(random_key) + crypted.decode('iso-8859-1')
    return dy_base64(result, 's1')


def dy_base64(data: str, mindex: str, pad: str = "=") -> str:
    letter0 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
    letter1 = "Dkdpgh4ZKsQB80/Mfvw36XI1R25+WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe="
    letter2 = "Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe="
    letter3 = "ckdp1h4ZKsUB80/Mfvw36XIgR25+WQAlEi7NLboqYTOPuzmFjJnryx9HVGDaStCe"
    letter4 = "Dkdpgh2ZmsQB80/MfvV36XI1R45-WUAlEixNLwoqYTOPuzKFjJnry79HbGcaStCe"
    table = {"s0": letter0, "s1": letter1, "s2": letter2, "s3": letter3, "s4": letter4}[mindex]
    data_bytes = data.encode('iso-8859-1')
    out = ""
    i = 0
    while i < len(data_bytes):
        c1 = data_bytes[i]; i += 1
        c2 = data_bytes[i] if i < len(data_bytes) else None; i += 1
        c3 = data_bytes[i] if i < len(data_bytes) else None; i += 1
        b1 = (c1 & 0xff) << 16
        b2 = ((c2 & 0xff) << 8) if c2 is not None else 0
        b3 = (c3 & 0xff) if c3 is not None else 0
        combined = b1 | b2 | b3
        out += table[(combined >> 18) & 0x3f]
        out += table[(combined >> 12) & 0x3f]
        out += table[(combined >> 6) & 0x3f] if c2 is not None else pad
        out += table[combined & 0x3f] if c3 is not None else pad
    return out


def generate_x_bogus(path_and_query: str, salt: str = "") -> str:
    """生成 X-Bogus — 与 webmssdk frontierSign 等价。

    Args:
        path_and_query: 完整路径+查询串, 如 "/aweme/mid/video/sts2/?scene=web&aid=1128"
        salt: 可选 salt (一般传 "")
    """
    return xbogus_fun(1, False, 0, salt, path_and_query)


if __name__ == "__main__":
    uri = "/aweme/mid/video/sts2/?scene=web&aid=1128"
    xb = generate_x_bogus(uri)
    print(f"X-Bogus: {xb}")
    print(f"length: {len(xb)}")
