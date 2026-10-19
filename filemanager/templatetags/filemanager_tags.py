from django import template

from ..utils import display_name as _display_name

register = template.Library()


@register.filter
def display_name(user):
    """{{ user|display_name }}: the same name the PDF and emails use."""
    return _display_name(user)
