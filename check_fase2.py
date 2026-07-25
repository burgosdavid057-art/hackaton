"""Chequeo de la Fase 2: los dos patrones sembrados tienen que aparecer.

No usa LLM. Corre sobre lo que quedo en data/produccion.db despues del pipeline.

  R-02 (L2): 3 atascos el 2026-07-20 + 2 el 2026-07-23 = 5 en 2 turnos
  Burbuja de poliuretano (L1): el 2026-07-22, con 2 paradas de ajuste de molde
"""
import json
import sys

from produccion import db, herramientas


def linea(t=""):
    print(t)


def mostrar(r, titulo):
    linea(f"\n===== {titulo} =====")
    if not r.get("disponible"):
        linea(f"  NO DISPONIBLE: {r.get('motivo')}")
        return []
    d = r["datos"]
    cob = r.get("cobertura", {})
    per = r.get("periodo", {})
    linea(f"  periodo: {per.get('desde')} -> {per.get('hasta')} "
          f"(ventana {per.get('ventana_dias')}d, ambito {per.get('linea') or 'toda la planta'})")
    linea(f"  cobertura: {cob.get('turnos_encontrados')}/{cob.get('turnos_esperados')} turnos"
          f" | sin_clasificar={cob.get('sin_clasificar')} | parcial={cob.get('parcial')}")
    recs = d.get("recurrentes", [])
    linea(f"  grupos evaluados={d.get('grupos_evaluados')} | recurrentes={d.get('total_recurrentes')}"
          f" | min_repeticiones={d.get('min_repeticiones')}")
    if d.get("nota"):
        linea(f"  nota: {d['nota']}")
    for g in recs:
        linea(f"   - [{g.get('tipo')}] {g.get('causa') or g.get('causa_texto')!r}"
              f" @ {g.get('estacion') or 'sin estacion'} ({g.get('linea')})"
              f" x{g.get('repeticiones')}"
              f" | min={g.get('minutos_totales')} und={g.get('unidades_totales')}"
              f" | {g.get('primera_aparicion')}..{g.get('ultima_aparicion')}"
              f" | clasificada={g.get('clasificada')}")
    return recs


def texto(g):
    return " ".join(str(g.get(k) or "") for k in
                    ("causa", "causa_texto", "estacion", "linea")).lower()


def main():
    con = db.conectar()
    n_turnos = con.execute("SELECT COUNT(*) c FROM turnos").fetchone()["c"]
    n_par = con.execute("SELECT COUNT(*) c FROM paradas").fetchone()["c"]
    n_scr = con.execute("SELECT COUNT(*) c FROM scrap").fetchone()["c"]
    linea(f"DB: {n_turnos} turnos | {n_par} paradas | {n_scr} scrap")
    linea("\nturnos cargados:")
    for r in con.execute(
        "SELECT t.id,t.fecha,t.turno,l.nombre linea,t.unidades_plan,t.unidades_producidas "
        "FROM turnos t LEFT JOIN lineas l ON l.id=t.linea_id ORDER BY t.fecha,t.turno"
    ):
        linea(f"  #{r['id']} {r['fecha']} T{r['turno']} {r['linea']} "
              f"plan={r['unidades_plan']} real={r['unidades_producidas']}")
    con.close()

    # El default del contrato. Aqui es donde la R-02 tiene que salir.
    recs3 = mostrar(herramientas.causas_recurrentes(dias=7, min_repeticiones=3),
                    "causas_recurrentes(dias=7, min_repeticiones=3)")
    # Con umbral 2 debe aparecer tambien el patron de L1 (ajuste de molde x2).
    recs2 = mostrar(herramientas.causas_recurrentes(dias=7, min_repeticiones=2),
                    "causas_recurrentes(dias=7, min_repeticiones=2)")
    mostrar(herramientas.causas_recurrentes(dias=7, min_repeticiones=2, linea="L1"),
            "causas_recurrentes(min_repeticiones=2, linea=L1)")

    linea("\n===== VEREDICTO FASE 2 =====")
    fallos = []

    r02 = [g for g in recs3 if "remach" in texto(g) or "r-02" in texto(g)]
    if r02:
        g = r02[0]
        linea(f"  OK  R-02 detectada: x{g.get('repeticiones')} repeticiones, "
              f"{g.get('minutos_totales')} min")
        if (g.get("repeticiones") or 0) < 5:
            fallos.append(f"R-02 aparece con {g.get('repeticiones')} repeticiones, "
                          f"se sembraron 5 (3 el 20-jul + 2 el 23-jul)")
    else:
        fallos.append("R-02 NO aparece con min_repeticiones=3 — la Fase 2 no sirve")

    pu = [g for g in recs2 if any(k in texto(g) for k in
                                  ("poliuretano", "burbuja", "molde", "inyec"))]
    if pu:
        g = pu[0]
        linea(f"  OK  patron L1 poliuretano/molde detectado: x{g.get('repeticiones')}")
    else:
        fallos.append("patron de poliuretano/molde en L1 NO aparece ni con min_repeticiones=2")

    if fallos:
        linea("\n  FALLOS:")
        for f in fallos:
            linea(f"   X {f}")
        return 1
    linea("\n  Fase 2 OK: los dos patrones sembrados se detectan.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
