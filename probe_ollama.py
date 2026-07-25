"""Que acepta de verdad el endpoint /v1 de Ollama: num_ctx y apagar el thinking.

No es teoria: cada variante se manda y se mira que devuelve, y de paso se
consulta /api/ps para ver con que contexto quedo cargado el modelo.
"""
import json
import urllib.request

from dotenv import load_dotenv

load_dotenv()

from produccion import db, extraer, ingest, prompts  # noqa: E402
from pathlib import Path  # noqa: E402

texto, _ = ingest.a_texto(Path("produccion/inbox/turno_2026-07-22_L1_formato.txt"))
cliente, modelo, _ = extraer._cliente()
mensajes = [
    {"role": "system", "content": prompts.extractor(
        db.texto_causas_para_prompt(), db.texto_catalogo_para_prompt())},
    {"role": "user", "content": texto},
]


def ctx_cargado() -> str:
    try:
        with urllib.request.urlopen("http://localhost:11434/api/ps", timeout=5) as r:
            for m in json.load(r).get("models", []):
                if m.get("name", "").startswith(modelo.split(":")[0]):
                    return str(m.get("context_length"))
    except Exception as e:  # noqa: BLE001
        return f"?({type(e).__name__})"
    return "?"


VARIANTES = {
    "A. options.num_ctx (lo que hace hoy el codigo)":
        {"extra_body": {"options": {"num_ctx": 8192}}},
    "B. + chat_template_kwargs.enable_thinking=false":
        {"extra_body": {"options": {"num_ctx": 8192},
                        "chat_template_kwargs": {"enable_thinking": False}}},
    "C. + think=false (parametro nativo de Ollama)":
        {"extra_body": {"options": {"num_ctx": 8192}, "think": False}},
}

for nombre, extra in VARIANTES.items():
    print(f"\n{'='*68}\n{nombre}\n{'='*68}")
    try:
        r = cliente.chat.completions.create(
            model=modelo, messages=mensajes, temperature=0,
            response_format={"type": "json_object"}, **extra,
        )
        msg = r.choices[0].message
        content = msg.content or ""
        razon = (getattr(msg, "model_extra", None) or {}).get("reasoning") or ""
        u = r.usage
        print(f"  contexto cargado : {ctx_cargado()}")
        print(f"  content          : {len(content)} chars")
        print(f"  reasoning        : {len(razon)} chars")
        print(f"  tokens           : prompt={u.prompt_tokens} compl={u.completion_tokens}")
        datos = extraer._json_del_texto(content)
        if datos is None:
            print("  PARSEO           : NO  <-- inservible")
            continue
        ok, motivo = extraer._forma_valida(datos)
        if not ok:
            print(f"  PARSEO           : forma invalida ({motivo})")
            continue
        ts = ok.get("turnos") or []
        print(f"  PARSEO           : OK, {len(ts)} turno(s)")
        for t in ts:
            print(f"    {t.get('fecha')} T{t.get('turno')} {t.get('linea')} "
                  f"plan={t.get('unidades_plan')} real={t.get('unidades_producidas')} "
                  f"paradas={len(t.get('paradas') or [])} scrap={len(t.get('scrap') or [])}")
    except Exception as e:  # noqa: BLE001
        print(f"  ERROR: {type(e).__name__}: {str(e)[:300]}")
