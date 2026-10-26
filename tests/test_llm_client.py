"""Tests du client LLM : normalisation, outils, flux et erreurs traduites.

Aucun appel reseau : le SDK ``openai`` est remplace par un objet scripte.
"""

from types import SimpleNamespace as NS

import pytest

from src.config import ConfigError, Settings
from src.llm_client import LLMClient, LLMError
from src.tools import Tool


def faux_sdk(reponse=None, erreur=None, flux=None):
    """Construit un faux SDK exposant ``chat.completions.create``."""

    appels = []

    def create(**params):
        appels.append(params)
        if erreur is not None:
            raise erreur
        return flux if flux is not None else reponse

    create.appels = appels
    return NS(chat=NS(completions=NS(create=create)))


def completion(texte=None, tool_calls=None, usage=(5, 2, 7), finish="stop"):
    """Reponse du SDK, sous la forme d'un objet a attributs."""
    message = NS(content=texte, tool_calls=tool_calls)
    consommation = (
        NS(
            prompt_tokens=usage[0],
            completion_tokens=usage[1],
            total_tokens=usage[2],
        )
        if usage is not None
        else None
    )
    return NS(
        choices=[NS(message=message, finish_reason=finish)],
        model="fake-model",
        usage=consommation,
    )


def appel_outil(nom="read_file", arguments='{"path": "README.md"}', call_id="c1"):
    return NS(id=call_id, function=NS(name=nom, arguments=arguments))


def test_reponse_texte(settings):
    sdk = faux_sdk(completion("  Bonjour  "))
    client = LLMClient(settings, client=sdk)

    reponse = client.chat([{"role": "user", "content": "salut"}])

    assert reponse.text == "Bonjour"
    assert reponse.tool_calls == []
    assert reponse.finish_reason == "stop"
    assert reponse.model == "fake-model"
    assert (reponse.prompt_tokens, reponse.completion_tokens, reponse.total_tokens) == (
        5,
        2,
        7,
    )
    assert client.tokens_used == 7


def test_appel_outil_parse(settings):
    sdk = faux_sdk(completion(tool_calls=[appel_outil()], finish="tool_calls"))
    client = LLMClient(settings, client=sdk)

    reponse = client.chat([{"role": "user", "content": "lis"}])

    assert reponse.has_tool_calls is True
    appel = reponse.tool_calls[0]
    assert appel.name == "read_file"
    assert appel.arguments == {"path": "README.md"}
    assert appel.is_valid is True
    assert appel.to_dict() == {
        "id": "c1",
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"path": "README.md"}'},
    }
    assert reponse.to_assistant_message()["tool_calls"][0]["id"] == "c1"


def test_arguments_json_invalide(settings):
    sdk = faux_sdk(completion(tool_calls=[appel_outil(arguments='{"path": ')]))
    client = LLMClient(settings, client=sdk)

    appel = client.chat([{"role": "user", "content": "lis"}]).tool_calls[0]

    assert appel.is_valid is False
    assert "invalide" in (appel.arguments_error or "")
    assert appel.raw_arguments == '{"path": '
    assert appel.to_dict()["function"]["arguments"] == '{"path": '


def test_arguments_json_non_objet(settings):
    sdk = faux_sdk(completion(tool_calls=[appel_outil(arguments="[1, 2]")]))
    client = LLMClient(settings, client=sdk)

    appel = client.chat([{"role": "user", "content": "lis"}]).tool_calls[0]

    assert "objet JSON" in (appel.arguments_error or "")


def test_appel_outil_sans_arguments(settings):
    sdk = faux_sdk(completion(tool_calls=[appel_outil(arguments="")]))
    client = LLMClient(settings, client=sdk)

    appel = client.chat([{"role": "user", "content": "lis"}]).tool_calls[0]

    assert appel.arguments == {}
    assert appel.arguments_error is None
    assert appel.is_valid is True


def test_reponse_sans_choices(settings):
    sdk = faux_sdk(NS(choices=[], model="fake-model", usage=None))
    client = LLMClient(settings, client=sdk)

    with pytest.raises(LLMError) as info:
        client.chat([{"role": "user", "content": "salut"}])

    assert "choices" in str(info.value)


def test_parametres_envoyes(settings):
    sdk = faux_sdk(completion("ok"))
    client = LLMClient(settings, client=sdk)
    outil = Tool(
        "ping",
        "Test.",
        {"type": "object", "properties": {}},
        lambda: "pong",
    )

    client.chat([{"role": "user", "content": "salut"}], tools=[outil])

    params = sdk.chat.completions.create.appels[0]
    assert params["model"] == "fake-model"
    assert params["messages"] == [{"role": "user", "content": "salut"}]
    assert params["temperature"] == 0.0
    assert params["max_tokens"] == 512
    assert params["tool_choice"] == "auto"
    assert params["tools"][0]["function"]["name"] == "ping"


def test_sans_outils_ni_tool_choice(settings):
    sdk = faux_sdk(completion("ok"))
    client = LLMClient(settings, client=sdk)

    client.chat([{"role": "user", "content": "salut"}])

    params = sdk.chat.completions.create.appels[0]
    assert "tools" not in params
    assert "tool_choice" not in params


def test_temperature_omise():
    sdk = faux_sdk(completion("ok"))
    client = LLMClient(Settings(temperature=None), client=sdk)

    client.chat([{"role": "user", "content": "salut"}])

    params = sdk.chat.completions.create.appels[0]
    assert "temperature" not in params


def test_extra_params_ajoute_et_retire(settings):
    sdk = faux_sdk(completion("ok"))
    client = LLMClient(settings, client=sdk)

    client.chat(
        [{"role": "user", "content": "salut"}],
        extra_params={"max_tokens": None, "max_completion_tokens": 8192},
    )

    params = sdk.chat.completions.create.appels[0]
    assert "max_tokens" not in params
    assert params["max_completion_tokens"] == 8192


def test_messages_vides(settings):
    client = LLMClient(settings, client=faux_sdk(completion("ok")))

    with pytest.raises(ValueError):
        client.chat([])


def test_usage_absent(settings):
    sdk = faux_sdk(completion("ok", usage=None))
    client = LLMClient(settings, client=sdk)

    reponse = client.chat([{"role": "user", "content": "salut"}])

    assert reponse.total_tokens == 0
    assert client.tokens_used == 0


def test_compteur_cumule_et_remis_a_zero(settings):
    sdk = faux_sdk(completion("ok"))
    client = LLMClient(settings, client=sdk)

    client.chat([{"role": "user", "content": "un"}])
    client.chat([{"role": "user", "content": "deux"}])
    assert client.tokens_used == 14

    client.reset_token_counter()
    assert client.tokens_used == 0


@pytest.mark.parametrize(
    "nom, extrait",
    [
        ("AuthenticationError", "401"),
        ("PermissionDeniedError", "403"),
        ("NotFoundError", "404"),
        ("RateLimitError", "429"),
        ("BadRequestError", "400"),
        ("APIConnectionError", "Impossible de joindre"),
        ("APITimeoutError", "Impossible de joindre"),
        ("ErreurInconnue", "ErreurInconnue"),
    ],
)
def test_erreurs_traduites(settings, nom, extrait):
    erreur = type(nom, (Exception,), {})(f"{nom} survenu")
    client = LLMClient(settings, client=faux_sdk(erreur=erreur))

    with pytest.raises(LLMError) as info:
        client.chat([{"role": "user", "content": "salut"}])

    assert extrait in str(info.value)


def test_flux_texte_et_outils(settings):
    fragments = [
        NS(
            model="fake-model",
            usage=None,
            choices=[NS(delta=NS(content="Bon", tool_calls=None), finish_reason=None)],
        ),
        NS(
            model="fake-model",
            usage=None,
            choices=[NS(delta=NS(content="jour", tool_calls=None), finish_reason=None)],
        ),
        NS(
            model="fake-model",
            usage=None,
            choices=[
                NS(
                    delta=NS(
                        content=None,
                        tool_calls=[
                            NS(
                                index=0,
                                id="c1",
                                function=NS(name="read_", arguments='{"pa'),
                            )
                        ],
                    ),
                    finish_reason=None,
                )
            ],
        ),
        NS(
            model="fake-model",
            usage=NS(prompt_tokens=6, completion_tokens=3, total_tokens=9),
            choices=[
                NS(
                    delta=NS(
                        content=None,
                        tool_calls=[
                            NS(
                                index=0,
                                id=None,
                                function=NS(name="file", arguments='th": "README.md"}'),
                            )
                        ],
                    ),
                    finish_reason="tool_calls",
                )
            ],
        ),
    ]
    vus = []
    sdk = faux_sdk(flux=iter(fragments))
    client = LLMClient(settings, client=sdk)

    reponse = client.chat(
        [{"role": "user", "content": "lis"}], stream=True, on_token=vus.append
    )

    assert vus == ["Bon", "jour"]
    assert reponse.text == "Bonjour"
    assert reponse.tool_calls[0].name == "read_file"
    assert reponse.tool_calls[0].arguments == {"path": "README.md"}
    assert reponse.total_tokens == 9
    assert sdk.chat.completions.create.appels[0]["stream"] is True


def test_check_connection(settings):
    sdk = faux_sdk(completion("OK"))
    client = LLMClient(settings, client=sdk)

    assert client.check_connection() == "OK"

    params = sdk.chat.completions.create.appels[0]
    assert params["max_tokens"] == 16
    assert params["temperature"] == 0


def test_cle_api_absente_pour_api_distante():
    client = LLMClient(Settings(api_key=None), client=None)

    with pytest.raises(ConfigError):
        _ = client.client


def test_repr_ne_divulgue_pas_la_cle(settings):
    client = LLMClient(settings, client=faux_sdk(completion("ok")))

    assert "sk-test" not in repr(client)
    assert "api_key=***" in repr(client)

