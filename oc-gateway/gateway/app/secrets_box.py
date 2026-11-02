from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    """Encrypts provider API keys at rest (Fernet = AES-128-CBC + HMAC-SHA256)."""

    def __init__(self, master_key):
        if not master_key:
            raise RuntimeError("GATEWAY_MASTER_KEY is not set (generate one with tools/genkeys.py)")
        self._f = Fernet(master_key.encode() if isinstance(master_key, str) else master_key)

    @staticmethod
    def generate():
        return Fernet.generate_key().decode()

    def encrypt(self, plaintext):
        return self._f.encrypt(plaintext.encode()).decode()

    def decrypt(self, token):
        try:
            return self._f.decrypt(token.encode()).decode()
        except InvalidToken as e:
            raise RuntimeError("cannot decrypt stored key: wrong GATEWAY_MASTER_KEY?") from e
