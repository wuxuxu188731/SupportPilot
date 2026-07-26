import bcrypt

MIN_PASSWORD_BYTES = 8
MAX_PASSWORD_BYTES = 72
BCRYPT_ROUNDS = 12

class PasswordPolicyError(ValueError):
  pass

def _password_bytes(password : str) -> bytes :
  encoded = password.encode("utf-8")
  if not MIN_PASSWORD_BYTES <= len(encoded) <= MAX_PASSWORD_BYTES:
    raise PasswordPolicyError("password must contain between 8 and 72 utf-8 bytes")
  return encoded

#对密码进行哈希，加密存储
def hash_password(password : str) -> str:
  encoded = _password_bytes(password)
  return bcrypt.hashpw(
    password=encoded,
    salt=bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
  ).decode("utf-8")

#验证密码，验证登录密码与数据库里面的哈希密码是否匹配
def verify_password(password : str ,password_hash : str) -> bool:
  try:
    encoded = _password_bytes(password)
    return bcrypt.checkpw(encoded,password_hash.encode("utf-8"))
  except (PasswordPolicyError,ValueError) as exc:
    return False