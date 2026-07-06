from __future__ import annotations

from dataclasses import dataclass
import csv
from functools import lru_cache
from pathlib import Path
import re


@dataclass(frozen=True)
class BirdTaxon:
    common_name: str
    scientific_name: str | None = None
    genus: str | None = None
    family: str | None = None
    order: str | None = None
    group: str | None = None


RANKS = ("genus", "family", "order", "group")

DEFAULT_TAXONOMY_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "birdnet" / "ebird_clements_taxonomy_v2025.csv"
)


COMMON_GROUP_RULES: tuple[tuple[tuple[str, ...], str, str | None, str | None], ...] = (
    (("macaw",), "macaw", "Psittacidae", "Psittaciformes"),
    (("parrot", "parakeet", "amazon"), "parrot/parakeet", "Psittacidae", "Psittaciformes"),
    (("woodpecker", "flicker", "piculet"), "woodpecker", "Picidae", "Piciformes"),
    (("owl", "owlet"), "owl", "Strigidae", "Strigiformes"),
    (("nightjar", "nighthawk", "pauraque", "potoo"), "nightjar/potoo", "Caprimulgidae", "Caprimulgiformes"),
    (("heron", "egret", "bittern"), "heron/egret", "Ardeidae", "Pelecaniformes"),
    (("ibis", "spoonbill"), "ibis/spoonbill", "Threskiornithidae", "Pelecaniformes"),
    (("chachalaca", "guan", "curassow"), "chachalaca/guan/curassow", "Cracidae", "Galliformes"),
    (("tinamou",), "tinamou", "Tinamidae", "Tinamiformes"),
    (("hummingbird", "hermit", "mango", "sapphire", "emerald"), "hummingbird", "Trochilidae", "Apodiformes"),
    (("trogon",), "trogon", "Trogonidae", "Trogoniformes"),
    (("motmot",), "motmot", "Momotidae", "Coraciiformes"),
    (("kingfisher",), "kingfisher", "Alcedinidae", "Coraciiformes"),
    (("toucan", "aracari"), "toucan/aracari", "Ramphastidae", "Piciformes"),
    (("jacamar",), "jacamar", "Galbulidae", "Piciformes"),
    (("puffbird", "nunbird", "nunlet"), "puffbird/nunbird", "Bucconidae", "Piciformes"),
    (("antbird", "antwren", "antshrike", "antvireo", "fire-eye"), "antbird", "Thamnophilidae", "Passeriformes"),
    (("woodcreeper",), "woodcreeper", "Dendrocolaptidae", "Passeriformes"),
    (("hornero", "spinetail", "foliage-gleaner", "thornbird", "xenops"), "ovenbird/woodcreeper", "Furnariidae", "Passeriformes"),
    (("flycatcher", "tody-flycatcher", "tyrant", "elaenia", "pewee", "kingbird", "kiskadee"), "flycatcher", "Tyrannidae", "Passeriformes"),
    (("manakin",), "manakin", "Pipridae", "Passeriformes"),
    (("cotinga", "becard", "tityra"), "cotinga/tityra", "Cotingidae", "Passeriformes"),
    (("vireo", "greenlet", "peppershrike"), "vireo/peppershrike", "Vireonidae", "Passeriformes"),
    (("wren",), "wren", "Troglodytidae", "Passeriformes"),
    (("thrush", "solitaire"), "thrush", "Turdidae", "Passeriformes"),
    (("tanager", "dacnis", "honeycreeper", "saltator", "seedeater", "grassquit"), "tanager/finch", "Thraupidae", "Passeriformes"),
    (("oriole", "cacique", "oropendola", "cowbird", "blackbird"), "icterid", "Icteridae", "Passeriformes"),
    (("warbler", "redstart", "waterthrush"), "warbler", "Parulidae", "Passeriformes"),
    (("sparrow", "finch", "euphonia"), "sparrow/finch", None, "Passeriformes"),
    (("crow", "jay"), "crow/jay", "Corvidae", "Passeriformes"),
    (("hawk", "eagle", "kite", "harrier"), "hawk/eagle/kite", "Accipitridae", "Accipitriformes"),
    (("falcon", "caracara"), "falcon/caracara", "Falconidae", "Falconiformes"),
    (("vulture",), "vulture", None, None),
    (("sandpiper", "yellowlegs", "snipe", "dowitcher"), "shorebird", "Scolopacidae", "Charadriiformes"),
    (("plover", "lapwing"), "shorebird", "Charadriidae", "Charadriiformes"),
    (("gull", "tern", "skimmer"), "gull/tern/skimmer", "Laridae", "Charadriiformes"),
    (("duck", "teal", "wigeon", "screamer"), "waterfowl", "Anatidae", "Anseriformes"),
    (("dove", "pigeon"), "dove/pigeon", "Columbidae", "Columbiformes"),
    (("cuckoo", "ani"), "cuckoo/ani", "Cuculidae", "Cuculiformes"),
    (("rail", "gallinule", "coot", "crake"), "rail/gallinule", "Rallidae", "Gruiformes"),
)


FAMILY_ORDER_HINTS = {
    "Psittacidae": "Psittaciformes",
    "Picidae": "Piciformes",
    "Strigidae": "Strigiformes",
    "Caprimulgidae": "Caprimulgiformes",
    "Ardeidae": "Pelecaniformes",
    "Threskiornithidae": "Pelecaniformes",
    "Cracidae": "Galliformes",
    "Tinamidae": "Tinamiformes",
    "Trochilidae": "Apodiformes",
    "Trogonidae": "Trogoniformes",
    "Momotidae": "Coraciiformes",
    "Alcedinidae": "Coraciiformes",
    "Ramphastidae": "Piciformes",
    "Galbulidae": "Piciformes",
    "Bucconidae": "Piciformes",
    "Thamnophilidae": "Passeriformes",
    "Dendrocolaptidae": "Passeriformes",
    "Furnariidae": "Passeriformes",
    "Tyrannidae": "Passeriformes",
    "Pipridae": "Passeriformes",
    "Cotingidae": "Passeriformes",
    "Vireonidae": "Passeriformes",
    "Troglodytidae": "Passeriformes",
    "Turdidae": "Passeriformes",
    "Thraupidae": "Passeriformes",
    "Icteridae": "Passeriformes",
    "Parulidae": "Passeriformes",
    "Corvidae": "Passeriformes",
    "Accipitridae": "Accipitriformes",
    "Falconidae": "Falconiformes",
    "Scolopacidae": "Charadriiformes",
    "Charadriidae": "Charadriiformes",
    "Laridae": "Charadriiformes",
    "Anatidae": "Anseriformes",
    "Columbidae": "Columbiformes",
    "Cuculidae": "Cuculiformes",
    "Rallidae": "Gruiformes",
}


def normalize_common_name(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.replace("_", " ").strip()).casefold()


def normalize_scientific_name(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.strip()).casefold()


def _merge_taxa(primary: BirdTaxon, fallback: BirdTaxon) -> BirdTaxon:
    return BirdTaxon(
        common_name=primary.common_name or fallback.common_name,
        scientific_name=primary.scientific_name or fallback.scientific_name,
        genus=primary.genus or fallback.genus,
        family=primary.family or fallback.family,
        order=primary.order or fallback.order,
        group=primary.group or fallback.group,
    )


def _taxonomy_path_key(taxonomy_path: str | None) -> str:
    path = Path(taxonomy_path).expanduser() if taxonomy_path else DEFAULT_TAXONOMY_PATH
    return str(path)


@lru_cache(maxsize=4)
def load_ebird_clements_taxa(
    taxonomy_path: str | None = None,
) -> tuple[dict[str, BirdTaxon], dict[str, BirdTaxon], dict[str, BirdTaxon]]:
    path = Path(_taxonomy_path_key(taxonomy_path))
    if not path.exists():
        return {}, {}, {}

    by_common: dict[str, BirdTaxon] = {}
    by_scientific: dict[str, BirdTaxon] = {}
    genus_candidates: dict[str, set[tuple[str, str]]] = {}
    genus_examples: dict[str, BirdTaxon] = {}

    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            common_name = (row.get("common_name") or "").strip()
            scientific_name = (row.get("scientific_name") or "").strip()
            genus = (row.get("genus") or "").strip()
            family = (row.get("family") or "").strip()
            order = (row.get("order") or "").strip()
            if not common_name or not genus or not family or not order:
                continue

            heuristic = infer_taxon_from_common_name(common_name)
            taxon = BirdTaxon(
                common_name=common_name,
                scientific_name=scientific_name or None,
                genus=genus,
                family=family,
                order=order,
                group=heuristic.group,
            )
            by_common.setdefault(normalize_common_name(common_name), taxon)
            if scientific_name:
                by_scientific.setdefault(normalize_scientific_name(scientific_name), taxon)
            genus_key = normalize_scientific_name(genus)
            genus_candidates.setdefault(genus_key, set()).add((family, order))
            genus_examples.setdefault(genus_key, taxon)

    by_genus = {
        genus_key: genus_examples[genus_key]
        for genus_key, family_orders in genus_candidates.items()
        if len(family_orders) == 1
    }
    return by_common, by_scientific, by_genus


@lru_cache(maxsize=16)
def load_species_taxa(species_list_path: str | None) -> dict[str, BirdTaxon]:
    if not species_list_path:
        return {}
    path = Path(species_list_path).expanduser()
    if not path.exists():
        return {}
    taxa: dict[str, BirdTaxon] = {}
    for line in path.read_text(errors="ignore").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        scientific_name: str | None = None
        common_name = text
        if "_" in text:
            scientific_name, common_name = (part.strip() for part in text.split("_", 1))
        genus = scientific_name.split()[0] if scientific_name and " " in scientific_name else None
        inferred = infer_taxon_from_common_name(common_name)
        taxon = BirdTaxon(
            common_name=common_name,
            scientific_name=scientific_name,
            genus=genus,
            family=inferred.family,
            order=inferred.order,
            group=inferred.group,
        )
        taxa[normalize_common_name(common_name)] = taxon
    return taxa


def resolve_taxon(
    common_name: str,
    species_list_path: str | None = None,
    taxonomy_path: str | None = None,
) -> BirdTaxon:
    by_common, by_scientific, by_genus = load_ebird_clements_taxa(_taxonomy_path_key(taxonomy_path))
    inferred = infer_taxon_from_common_name(common_name)
    key = normalize_common_name(common_name)
    if key in by_common:
        return _merge_taxa(by_common[key], inferred)

    taxa = load_species_taxa(str(species_list_path) if species_list_path else None)
    if key in taxa:
        species_taxon = taxa[key]
        if species_taxon.scientific_name:
            scientific_key = normalize_scientific_name(species_taxon.scientific_name)
            if scientific_key in by_scientific:
                return _merge_taxa(by_scientific[scientific_key], inferred)
        if species_taxon.genus:
            genus_key = normalize_scientific_name(species_taxon.genus)
            if genus_key in by_genus:
                genus_taxon = by_genus[genus_key]
                return BirdTaxon(
                    common_name=species_taxon.common_name,
                    scientific_name=species_taxon.scientific_name,
                    genus=species_taxon.genus,
                    family=genus_taxon.family,
                    order=genus_taxon.order,
                    group=inferred.group or genus_taxon.group,
                )
        return species_taxon

    return BirdTaxon(
        common_name=common_name,
        genus=inferred.genus,
        family=inferred.family,
        order=inferred.order,
        group=inferred.group,
    )


def infer_taxon_from_common_name(common_name: str) -> BirdTaxon:
    key = normalize_common_name(common_name)
    for terms, group, family, order in COMMON_GROUP_RULES:
        if any(term in key for term in terms):
            return BirdTaxon(
                common_name=common_name,
                family=family,
                order=order or (FAMILY_ORDER_HINTS.get(family) if family else None),
                group=group,
            )
    return BirdTaxon(common_name=common_name)


def taxon_rank_value(taxon: BirdTaxon, rank: str) -> str | None:
    value = getattr(taxon, rank, None)
    return value if isinstance(value, str) and value else None
