"""Tests du socle des outils : validation des arguments et registre."""

import pytest

from src.tools import Tool, ToolRegistry, build_default_registry
from src.tools.base import ToolError, ToolResult, validate_arguments

SCHEMA_PING = {
    "type": "object",
    "properties": {
        "x": {"type": "integer"},
        "mode": {"type": "string", "enum": ["a", "b"]},
    },
    "required": ["x"],
    "additionalProperties": False,
}


def _outil(handler=None, schema=None, name="ping"):
    return Tool(
        name=name,
        description="Outil de test.",
        parameters=schema or SCHEMA_PING,
        handler=handler or (lambda **kwargs: "pong"),
    )


# --- registre par defaut ---------------------------------------------------


def test_cinq_outils_par_defaut(registry):
    assert registry.names == [
        "list_dir",
        "read_file",
        "run_command",
        "search_code",
        "write_file",
    ]
    assert len(registry) == 5


def test_schemas_au_format_function(registry):
    schemas = registry.schemas

    assert len(schemas) == 5
    for schema in schemas:
        assert schema["type"] == "function"
        assert schema["function"]["name"]
        assert schema["function"]["parameters"]["type"] == "object"


def test_registre_par_defaut_expose_les_noms():
    from src.tools import TOOL_NAMES

    registre = build_default_registry()

    assert tuple(registre.names) == tuple(sorted(TOOL_NAMES))
    assert "run_command" in registre


def test_outil_inconnu(registry):
    resultat = registry.execute("rm_rf", {"path": "/"})

    assert resultat.ok is False
    assert "Outil inconnu" in resultat.content
    assert "read_file" in resultat.content


# --- validation des arguments ---------------------------------------------


@pytest.mark.parametrize(
    "arguments, extrait",
    [
        ({"x": 1, "inconnu": 2}, "inconnu"),
        ({}, "obligatoire"),
        ({"x": "texte"}, "type integer"),
        ({"x": True}, "booléen"),
        ({"x": 1, "mode": "z"}, "l'une de ces valeurs"),
        ("pas un objet", "objet JSON"),
    ],
)
def test_validation_refuse_les_mauvais_arguments(arguments, extrait):
    with pytest.raises(ToolError) as info:
        validate_arguments(SCHEMA_PING, arguments, "ping")

    assert extrait in str(info.value)


def test_validation_retourne_un_nouveau_dict():
    arguments = {"x": 3}
    nettoye = validate_arguments(SCHEMA_PING, arguments, "ping")

    assert nettoye == {"x": 3}
    assert nettoye is not arguments


def test_validation_ignore_les_options_nulles():
    assert validate_arguments(SCHEMA_PING, {"x": 1, "mode": None}, "ping") == {"x": 1}


def test_validation_none_signale_les_obligatoires():
    with pytest.raises(ToolError) as info:
        validate_arguments(SCHEMA_PING, None, "ping")

    assert "obligatoire" in str(info.value)


def test_handler_non_appele_si_arguments_invalides():
    appels = []

    def handler(**kwargs):
        appels.append(kwargs)
        return "ok"

    registre = ToolRegistry([_outil(handler)])
    resultat = registre.execute("ping", {"x": "pas un entier"})

    assert resultat.ok is False
    assert appels == []


def test_handler_appele_avec_arguments_valides():
    registre = ToolRegistry([_outil()])

    resultat = registre.execute("ping", {"x": 5})

    assert resultat.ok is True
    assert resultat.content == "pong"
    assert resultat.error is None


def test_exception_inattendue_ne_casse_pas_le_registre():
    def handler(**kwargs):
        raise RuntimeError("boum")

    registre = ToolRegistry([_outil(handler)])
    resultat = registre.execute("ping", {"x": 1})

    assert resultat.ok is False
    assert "ERREUR inattendue" in resultat.content
    assert "RuntimeError" in resultat.content


def test_signature_incompatible():
    registre = ToolRegistry([_outil(lambda: "sans argument")])

    resultat = registre.execute("ping", {"x": 1})

    assert resultat.ok is False
    assert "arguments incompatibles" in resultat.content


# --- outils et resultats ---------------------------------------------------


def test_tool_result_message_openai():
    resultat = ToolResult(name="ping", ok=True, content="pong")

    assert resultat.to_message("call-9") == {
        "role": "tool",
        "tool_call_id": "call-9",
        "content": "pong",
    }


@pytest.mark.parametrize(
    "name, schema, handler",
    [
        ("nom avec espace", SCHEMA_PING, lambda **kwargs: "ok"),
        ("ok", {"type": "string"}, lambda **kwargs: "ok"),
        ("ok", SCHEMA_PING, "pas appelable"),
    ],
)
def test_tool_invalide(name, schema, handler):
    with pytest.raises(ValueError):
        Tool(name=name, description="Test.", parameters=schema, handler=handler)


def test_enregistrement_doublon_refuse():
    registre = ToolRegistry([_outil()])

    with pytest.raises(ValueError):
        registre.register(_outil())


def test_enregistrement_type_invalide():
    with pytest.raises(TypeError):
        ToolRegistry().register({"name": "ping"})


def test_contain_et_iteration(registry):
    assert "read_file" in registry
    assert "rm_rf" not in registry
    assert 42 not in registry
    assert [tool.name for tool in registry] == registry.names


def test_get_retourne_l_outil(registry):
    outil = registry.get("read_file")

    assert outil.name == "read_file"
    assert outil.schema["function"]["name"] == "read_file"
    assert registry.get("run_command").dangerous is True
    assert outil.dangerous is False
