from allianceauth import hooks
from allianceauth.services.hooks import UrlHook, MenuItemHook
from django.utils.translation import gettext_lazy as _

from . import urls as codex_urls


@hooks.register("url_hook")
def register_urls():
    return UrlHook(codex_urls, "codex", r"^codex/")


class CodexMainMenu(MenuItemHook):
    def __init__(self):
        MenuItemHook.__init__(
            self,
            _("Corp Codex"),
            "fa-solid fa-users",
            "codex:index",
            navactive=["codex:index"]
        )

    def render(self, request):
        if request.user.is_staff or request.user.has_perm("codex.view_corpmember"):
            return MenuItemHook.render(self, request)
        return ""


@hooks.register("menu_item_hook")
def register_codex_menu():
    return CodexMainMenu()
