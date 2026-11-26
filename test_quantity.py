"""Validaciones de cantidad declarada y del relato que sustenta las líneas."""
import pytest

from test_cases import act, opening, post, team  # pytest fixture


@pytest.mark.parametrize("unit", ["unidades", "CAJAS", "display", "baldes", "tambor", "tanques", "caja de 12"])
def test_whole_containers_cannot_have_fractional_claims(team, unit):
    data = opening()
    data["lines"][0].update(quantity=1.5, unit=unit)
    error = post(team["internacional"], "/api/cases", data, expected=400)
    assert "entera" in error["error"]
    assert team["internacional"].get("/api/cases").json == []


def test_volume_and_common_reason(team):
    area = team["nacional"]
    data = opening()
    data["lines"][0].update(quantity=1.375, unit="litro", reason="")
    case = post(area, "/api/cases", data)
    sent = act(area, case, "submit")
    assert sent["status"] == "enviado"
    assert sent["lines"][0]["reason"] == ""
    assert sent["description"] == data["description"]
    assert sent["lines"][0]["quantity"] == 1.375


def test_line_reasons_or_common_account(team):
    area = team["internacional"]
    data = opening()
    data["description"] = ""
    c = post(area, "/api/cases", data)
    assert act(area, c, "submit")["status"] == "enviado"
    data["lines"][0]["reason"] = ""
    incomplete = post(area, "/api/cases", data)
    assert "motivo" in act(area, incomplete, "submit", expect=400)["error"]


def test_decision_and_dispatch_keep_the_claimed_unit(team):
    area = team["nacional"]
    sent = act(area, post(area, "/api/cases", opening()), "submit")
    lid = sent["lines"][0]["id"]
    assignment = dict(line_id=lid, authorized_by="Gerencia", note="Reposición autorizada")
    invalid = act(area, sent, "decision", expect=400, replacement=0.5, **assignment)
    assert "entera" in invalid["error"]
    decided = act(area, sent, "decision", replacement=1, **assignment)
    movement = dict(line_id=lid, date=sent["date"], support="Soporte ficticio")
    invalid = act(area, decided, "dispatch", expect=400, quantity=0.5, **movement)
    assert "entera" in invalid["error"]
    assert act(area, decided, "dispatch", quantity=1, **movement)["lines"][0]["dispatched"] == 1
