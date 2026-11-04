"""Shared Flask/web helpers used by every service — base template loader,
the "back to manager" link, and the page registry — so header/nav chrome
looks identical everywhere and adding a page doesn't mean editing routes
and nav links by hand each time (see ../ui-style.md).
"""

import re
from pathlib import Path

from flask import render_template
from jinja2 import ChoiceLoader, FileSystemLoader

from shared.config import load_config

SHARED_TEMPLATES = Path(__file__).resolve().parent / "web" / "templates"
SHARED_STATIC = Path(__file__).resolve().parent / "web" / "static"
TITLE_COMMENT = re.compile(r"\{#\s*title:\s*(.+?)\s*#\}")
NAV_HIDDEN_COMMENT = re.compile(r"\{#\s*nav:\s*hidden\s*#\}")


def use_shared_templates(app):
    """Let this app's templates extend shared/web/templates/base.html."""
    app.jinja_loader = ChoiceLoader([app.jinja_loader, FileSystemLoader(SHARED_TEMPLATES)])


def use_shared_static(app):
    """Serve shared/web/static (e.g. ws-utils.js) at this app's /static."""
    app.static_folder = str(SHARED_STATIC)


def manager_url(browser_host: str) -> str:
    manager_cfg = load_config()["services"]["manager"]
    return f"http://{browser_host}:{manager_cfg['port']}/"


def service_url(browser_host: str, service_name: str, scheme: str = "http") -> str:
    """Build a URL to another service's web UI, e.g. so a page's own JS can
    open a websocket straight to it (see aruco-prd.md — the Map page reads
    oxts-nav's /ws/nav directly rather than having aruco re-publish it)."""
    port = load_config()["services"][service_name]["port"]
    return f"{scheme}://{browser_host}:{port}"


def discover_pages(pages_dir: Path):
    """List (slug, title, hidden) for every template in a service's
    pages/ folder.

    Title comes from a `{# title: My Page #}` comment at the top of the
    template, falling back to the filename. This is what lets a page be
    added just by dropping a new template in — by hand, or later by an
    AI page-generator — with no code change needed to make it show up in
    the nav or be reachable.

    A page also carrying `{# nav: hidden #}` is still routable at
    /pages/<slug> exactly the same way, but register_pages() excludes it
    from the dropdown — for pages only ever meant to be entered from
    elsewhere (e.g. navigate's edit-path, reached via the Paths page's
    own Edit button with a `?name=` query string, not chosen directly
    from the nav)."""
    pages = []
    for path in sorted(Path(pages_dir).glob("*.html")):
        slug = path.stem
        text = path.read_text()
        match = TITLE_COMMENT.search(text)
        title = match.group(1) if match else slug.replace("-", " ").title()
        hidden = bool(NAV_HIDDEN_COMMENT.search(text))
        pages.append((slug, title, hidden))
    return pages


def register_pages(app, pages_dir: Path, index_slug: str, context_providers: dict = None):
    """Wire up generic routes for every template in pages_dir:

    - GET /              -> the page whose slug is index_slug
    - GET /pages/<slug>  -> that page

    `context_providers` maps a slug to a zero-arg callable returning extra
    template context for just that page (e.g. data a page needs beyond
    the shared nav/websocket wiring). A page not listed there just gets
    the common context — no entry needed for a simple page.
    """
    context_providers = context_providers or {}
    pages_dir = Path(pages_dir)

    def render(slug):
        extra = context_providers[slug]() if slug in context_providers else {}
        all_pages = discover_pages(pages_dir)
        nav_pages = [(s, title) for s, title, hidden in all_pages if not hidden]
        current_title = next((title for s, title, _hidden in all_pages if s == slug), slug)
        return render_template(
            f"pages/{slug}.html",
            pages=nav_pages,
            current_slug=slug,
            current_title=current_title,
            index_slug=index_slug,
            **extra,
        )

    @app.route("/")
    def _index_page():
        return render(index_slug)

    @app.route("/pages/<slug>")
    def _registered_page(slug):
        return render(slug)
