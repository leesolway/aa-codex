from django import template

register = template.Library()

EVE_IMAGE_BASE = "https://images.evetech.net"


@register.simple_tag
def eve_image(category, entity_id, size=32):
    """Return an EVE image URL.

    Usage:
        {% eve_image "character" character_id 128 %}
        {% eve_image "corporation" corporation_id %}
        {% eve_image "alliance" alliance_id 64 %}
    """
    if category == "character":
        return f"{EVE_IMAGE_BASE}/characters/{entity_id}/portrait?size={size}"
    elif category == "corporation":
        return f"{EVE_IMAGE_BASE}/corporations/{entity_id}/logo?size={size}"
    elif category == "alliance":
        return f"{EVE_IMAGE_BASE}/alliances/{entity_id}/logo?size={size}"
    return ""


@register.filter
def main_character_name(user):
    """Return the user's main character name, or username as fallback."""
    if user is None:
        return ""
    try:
        main = user.profile.main_character
        if main:
            return main.character_name
    except Exception:
        pass
    return user.username
