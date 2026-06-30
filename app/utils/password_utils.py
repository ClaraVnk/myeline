"""Password generation utilities."""

import secrets
import string


def generate_strong_password(length=12):
    """Generate a strong password with required complexity.

    Args:
        length (int): Password length (default: 12)

    Returns:
        str: Generated strong password
    """
    # Inclure au moins une majuscule, une minuscule, un chiffre et un caractère spécial.
    # IMPORTANT : `_` est exclu du set de specials car is_strong_password() le
    # considère comme un word-char via `[^\w\s]` — l'inclure ici provoquait des
    # passwords générés qui étaient rejetés par leur propre validateur.
    SPECIAL_CHARS = "!@#$%^&*()-+=<>?"
    uppercase = secrets.choice(string.ascii_uppercase)
    lowercase = secrets.choice(string.ascii_lowercase)
    digit = secrets.choice(string.digits)
    special = secrets.choice(SPECIAL_CHARS)

    # Remplir le reste avec des caractères aléatoires
    all_chars = string.ascii_letters + string.digits + SPECIAL_CHARS
    remaining = [secrets.choice(all_chars) for _ in range(length - 4)]
    pwd_list = [uppercase, lowercase, digit, special] + remaining
    # Use secrets.SystemRandom for secure shuffling
    import random
    rng = random.SystemRandom()
    rng.shuffle(pwd_list)
    return "".join(pwd_list)
