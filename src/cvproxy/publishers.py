"""Publisher allowlist and blocklist.

The allowlist limits the backfill jobs to the publishers worth fetching; an
empty allowlist means every publisher that isn't blocked.  Blocked publishers
are never stored and never served: their volumes are remembered in
``blocked_volume`` (issues only name their volume), cached rows are deleted
by the blocked-publisher purge job, and ``PublisherFilterMiddleware`` drops
them from every /api/ response, including ones fetched live from ComicVine.

The lists live in localcv.db (``publisher_list``) and are edited on the
dashboard's Publishers tab; SEED_ALLOW and SEED_BLOCK fill them once.
"""

from __future__ import annotations

import csv
import io
import json
from typing import TYPE_CHECKING, Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

if TYPE_CHECKING:
    from cvproxy.db import Database

#: First /api/ path segment -> resource type of its results (None: per result)
_PATH_KINDS: dict[str, str | None] = {
    "volume": "volume", "volumes": "volume",
    "issue": "issue", "issues": "issue",
    "publisher": "publisher", "publishers": "publisher",
    "search": None,
}


def _ref_id(value: Any) -> Any:
    return value.get("id") if isinstance(value, dict) else None


def is_blocked(kind: str | None, result: Any, db: Database) -> bool:
    """True when a CV result belongs to a blocked publisher."""
    if not isinstance(result, dict):
        return False
    kind = kind or result.get("resource_type")
    if kind == "volume":
        return (result.get("id") in db.blocked_volumes
                or _ref_id(result.get("publisher")) in db.blocked_publishers)
    if kind == "issue":
        return _ref_id(result.get("volume")) in db.blocked_volumes
    if kind == "publisher":
        return result.get("id") in db.blocked_publishers
    return False


def drop_blocked(kind: str | None, body: Any, db: Database) -> Any:
    """*body* (a CV envelope) without results of blocked publishers.

    A blocked single result becomes ComicVine's "Object Not Found" envelope.
    """
    if not isinstance(body, dict):
        return body
    results = body.get("results")
    if isinstance(results, dict):
        if not is_blocked(kind, results, db):
            return body
        return {
            "status_code": 101, "error": "Object Not Found", "number_of_total_results": 0,
            "number_of_page_results": 0, "limit": 0, "offset": 0, "results": [],
        }
    if not isinstance(results, list):
        return body
    kept = [r for r in results if not is_blocked(kind, r, db)]
    dropped = len(results) - len(kept)
    if not dropped:
        return body
    out = {**body, "results": kept, "number_of_page_results": len(kept)}
    if isinstance(body.get("number_of_total_results"), int):
        out["number_of_total_results"] = max(0, body["number_of_total_results"] - dropped)
    return out


def list_csv(entries: list[dict[str, Any]]) -> str:
    """A publisher list as CSV: header "id,name", one publisher per row."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["id", "name"])
    writer.writerows([p["id"], p["name"] or ""] for p in entries)
    return out.getvalue()


def parse_list_csv(text: str) -> tuple[dict[int, str | None], int]:
    """({id: name}, rows skipped) from CSV whose first column is a publisher ID.

    IDs may be ComicVine's "4010-123" form; an optional second column is the
    name; a header row and blank rows are ignored.
    """
    entries: dict[int, str | None] = {}
    skipped = 0
    for n, row in enumerate(csv.reader(io.StringIO(text))):
        if not row or not row[0].strip():
            continue
        raw = row[0].strip()
        if n == 0 and raw.lower() == "id":
            continue
        try:
            pid = int(raw.removeprefix("4010-"))
        except ValueError:
            skipped += 1
            continue
        entries[pid] = row[1].strip() or None if len(row) > 1 else None
    return entries, skipped


def _path_kind(path: str) -> tuple[bool, str | None]:
    """(whether the path serves CV results to filter, their resource type)."""
    if not path.startswith("/api/"):
        return False, None
    segment = path.removeprefix("/api/").split("/", 1)[0]
    return segment in _PATH_KINDS, _PATH_KINDS.get(segment)


class PublisherFilterMiddleware:
    """Drops blocked publishers' results from /api/ JSON responses.

    Sits inside XmlFormatMiddleware, so XML answers are filtered too.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        filtered, kind = _path_kind(scope["path"]) if scope["type"] == "http" else (False, None)
        db: Database | None = getattr(scope["app"].state, "db", None) if filtered else None
        if db is None or not (db.blocked_publishers or db.blocked_volumes):
            await self.app(scope, receive, send)
            return

        start: Message = {}
        chunks: list[bytes] = []

        async def capture(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        await self.app(scope, receive, capture)
        body = b"".join(chunks)
        headers = list(start.get("headers", []))
        if any(k.lower() == b"content-type" and b"json" in v for k, v in headers):
            try:
                data = json.loads(body)
            except ValueError:
                pass
            else:
                kept = drop_blocked(kind, data, db)
                if kept is not data:
                    body = json.dumps(kept).encode()
        headers = [(k, v) for k, v in headers if k.lower() != b"content-length"]
        headers.append((b"content-length", str(len(body)).encode()))
        await send({**start, "headers": headers})
        await send({"type": "http.response.body", "body": body})


# ComicVine publisher IDs; names as ComicVine spells them.
SEED_ALLOW: dict[int, str] = {
    # Requested
    31: "Marvel",
    10: "DC Comics",
    513: "Image",
    364: "Dark Horse Comics",
    1190: "IDW Publishing",
    1868: "Boom! Studios",
    1859: "Dynamite Entertainment",
    1924: "DMG/Valiant Entertainment",
    485: "Valiant/Acclaim",
    682: "Oni Press",
    101: "Archie Comics",
    4212: "Titan Comics",
    2104: "Aftershock Comics",
    7801: "Vault Comics",
    7385: "Mad Cave Studios",
    8600: "Ahoy Comics",
    309: "Fantagraphics",
    767: "Top Shelf",
}

SEED_BLOCK: dict[int, str] = {
    # Requested
    2350: "Panini Comics",
    2328: "Panini Verlag",
    2812: "Marvel UK/Panini UK",
    2338: "Panini España",
    2205: "Panini Brasil",
    2245: "Panini France",
    2456: "Editorial Panini México",
    1921: "Panini Rus",
    2772: "Franco Cosimo Panini",
    7321: "Panini Nederland",
    9283: "Panini Chile",
    2705: "Editorial Televisa",
    3253: "Editorial Televisa Chile",
    4788: "Urban Comics",
    5549: "RW Uitgeverij",
    6750: "RW Edizioni",
    2867: "Verlag Thomas Tilsner",
    2319: "Planeta DeAgostini",
    10950: "Grupo Planeta",
    11135: "Planeta Komiksów",
    2855: "Cross Cult",
    3432: "ECC Ediciones",
    2903: "Ediciones Zinco",
    2569: "Norma Editorial",
    538: "Semic/Nordisk Forlag AS",
    3089: "Semic Press AB",
    7654: "Semic Press Oy/Egmont Kustannus",
    4215: "TM-Semic",
    1133: "Semic S.A.",
    3427: "Semic Press",
    1239: "Semic International",
    2811: "Semic Española de Ediciones",
    6427: "Semic-Slovart",
    10940: "Egmont Comics",
    2515: "Egmont Ehapa Verlag",
    2066: "Egmont Publishing A/S",
    4771: "Egmont Polska",
    3218: "Egmont Publishing (UK)",
    11107: "Egmont ČR",
    3145: "Egmont Imagination India",
    11066: "Egmont Estonia",
    522: "Egmont Lietuva",
    8086: "Egmont FPC Pty Ltd",
    11556: "Egmont Hrvatska",
    2638: "Egmont Bulgaria",
    5359: "Egmont Romania",
    174: "Carlsen Verlag",
    4198: "Carlsen/if",
    246: "Carlsen Comics (DK)",
    808: "Bonnier Carlsen",
    3625: "Carlsen Forlag",
    2561: "Dino Comics",
    596: "Splitter",
    2498: "Hachette",
    120: "Arnoldo Mondadori Editore",
    2094: "Abril",
    3490: "Mythos Editora",
    2538: "Grupo Editorial Vid",
    2365: "Editorial Novaro",
    3008: "Ediciones Vértice",
    257: "Williams Förlag AB",
    11748: "Williams Forlag A/S",
    805: "Albert Bonniers Förlag",
    # Japanese originals
    1867: "Shueisha",
    2259: "Kodansha",
    2781: "Shogakukan",
    1977: "Akita Shoten",
    3602: "Houbunsha",
    2303: "Hakusensha",
    3496: "Takeshobo",
    2384: "Futabasha Publishers Ltd.",
    3691: "Square Enix",
    1961: "Kadokawa Shoten",
    3744: "Nihon Bungeisha",
    2814: "Shōnen Gahōsha",
    3169: "Media Factory",
    2824: "ASCII Media Works",
    3559: "Wani Magazine",
    2832: "Ichijinsha",
    2795: "Tokuma Shoten",
    3631: "Coremagazine",
    # German, Swiss and Nordic
    1100: "Bastei Verlag",
    5979: "Pabel Verlag",
    2382: "Condor Verlag",
    7933: "Gruner + Jahr",
    3808: "Tokyopop GmbH",
    2904: "Lehning Verlag",
    2392: "Neuer Tessloff Verlag",
    2677: "Norbert Hethke Verlag",
    3410: "Gevacur AG",
    3319: "Hjemmet",
    2508: "Allers Forlag",
    3888: "Illustrerte Klassikere",
    2745: "Se-Bladene",
    267: "Atlantic Förlags AB",
    4107: "Interpresse (DK)",
    # Italian
    308: "Eura Editoriale",
    2307: "Edizioni Star Comics",
    2009: "Ediperiodici",
    2008: "Edifumetto",
    100: "Sergio Bonelli Editore",
    225: "Casa Editrice Universo",
    184: "Editoriale Corno",
    3788: "Editoriale Cosmo",
    7049: "Edizioni Fratelli Spada",
    9767: "La Gazzetta dello Sport",
    189: "Casa Editrice Dardo",
    451: "Play Press",
    3828: "Ludens",
    302: "Comic Art",
    # French, Belgian and Québécois
    2368: "Dupuis",
    2030: "Dargaud",
    2320: "Le Lombard",
    2892: "Editions Lug",
    2691: "Éditions Glénat",
    2010: "Elvifrance",
    2894: "Mon Journal",
    2877: "Editions Vaillant",
    2890: "Arédit - Artima",
    2000: "Soleil",
    2923: "Delcourt",
    3159: "Pika Édition",
    6411: "Sage - Sagédition",
    3166: "Editions Héritage",
    7751: "Éditions des Remparts",
    8058: "Société Française de Presse Illustrée (SFPI)",
    4277: "Kurokawa",
    2579: "Casterman",
    4206: "Crunchyroll SA",
    9196: "Nobi Nobi",
    10793: "Meian",
    # Dutch and Flemish
    2505: "JuniorPress BV",
    10672: "DPG Media",
    2982: "Standaard Uitgeverij",
    2506: "De Vrijbuiter",
    4774: "Don Lawrence Collection",
    4658: "Talent",
    # Spanish and Latin American
    2965: "Editorial Bruguera",
    10504: "Producciones García Ferré",
    9479: "Editorial Valenciana",
    7438: "Milky Way Ediciones",
    2552: "Ediciones B",
    8847: "Ediciones Toray, S. A.",
    7441: "Hispano Americana de Ediciones",
    2673: "Editora Zig Zag",
    2671: "Editorial Greco (Cinco)",
    2681: "Editorial Toukan",
    7898: "Editorial Ivrea",
    10765: "Editorial AGA",
    9475: "Editorial Maga",
    3360: "Editorial Ferma",
    2683: "Promotora K",
    5802: "Epucol",
    10149: "ABC",
    # Portuguese and Brazilian
    6490: "Agência Portuguesa de Revistas",
    3415: "Editora Brasil-América Limitada",
    4088: "Empresa Nacional de Publicidade (ENP)",
    11134: "JBC",
    # Asian
    1926: "Jademan",
    4310: "Tong Li Publishing Co.",
    11233: "Jade Dynasty",
    2815: "Daewon C.I.",
    2280: "Raj Comics",
    1955: "Islas Filipinas Publishing Co.",
    # Balkan
    8984: "Vjesnik",
    9796: "Niro Dečje Novine",
    10256: "Marketprint",
    # UK and Australian reprints
    3207: "Alan Class Comics",
    3358: "Thorpe & Porter",
    2537: "Frew Publications",
    3274: "Horwitz",
    3225: "World Distributors",
    2361: "L. Miller & Son, Ltd",
    2378: "Murray Comics",
    2911: "Eaglemoss Publications",
}
