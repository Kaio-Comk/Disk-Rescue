"""
tests/test_i18n.py — Testes unitários do sistema de internacionalização e troca de idiomas.
"""

from core.i18n import (
    SUPPORTED_LANGUAGES,
    detect_system_language,
    get_current_language,
    set_language,
    t,
)


def test_i18n_default_and_switching():
    set_language("pt_BR")
    assert get_current_language() == "pt_BR"
    assert t("btn_hero_scan") == "EXECUTAR DIAGNÓSTICO COMPLETO (1-CLIQUE)"

    set_language("en_US")
    assert get_current_language() == "en_US"
    assert t("btn_hero_scan") == "RUN COMPLETE DIAGNOSTIC (1-CLICK)"

    set_language("es_ES")
    assert t("btn_hero_scan") == "EJECUTAR DIAGNÓSTICO COMPLETO (1-CLIC)"

    set_language("ru_RU")
    assert "ДИАГНОСТИК" in t("btn_hero_scan")

    set_language("zh_CN")
    assert "诊断" in t("btn_hero_scan")

    set_language("ja_JP")
    assert "診断" in t("btn_hero_scan")


def test_i18n_fallback_and_formatting():
    set_language("en_US")
    # Formatação com parâmetros
    msg = t("raw_status", status="OK")
    assert msg == "Raw: OK"

    # Chave inexistente retorna a própria chave
    assert t("chave_que_nao_existe_xyz") == "chave_que_nao_existe_xyz"


def test_i18n_detect_system_language():
    lang = detect_system_language()
    assert lang in SUPPORTED_LANGUAGES
