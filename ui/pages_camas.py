"""
ui/pages_camas.py — Mapa de camas por piso, habitación y unidad.

La ubicación sale del código de cama del HIS (H-203C = piso 2, habitación 203, cama C; G-108B = piso 1,
habitación 108, cama B). Las unidades sin ese patrón (UCI, intermedios, observación, recuperación) se muestran
por unidad. El estado es el censo del día de corte, con la misma regla de ocupación del tablero.
Las camas virtuales no se dibujan como camas: son la capacidad de expansión de cada unidad.
"""
from __future__ import annotations

import streamlit as st

import database as db
from agent import fmt_num
from ui import context as ctx
from ui.theme import AMBER, BORDER, EMERALD, MUTED, TEXT, chip, esc

SHORT_STAY_DAYS, LONG_STAY_DAYS = 10, 15
CRITICAL_WORDS = ("INTENSIV", "INTERMEDIO", "BASICO NEONATAL", "CUIDAD BASICO")

CSS = f"""
<style>
  .map-legend {{display:flex; gap:1rem; flex-wrap:wrap; font-size:0.78rem; color:{MUTED}; margin:0.2rem 0 0.8rem;}}
  .map-legend span {{display:inline-flex; align-items:center; gap:0.35rem;}}
  .sw {{width:0.85rem; height:0.85rem; border-radius:3px; display:inline-block;}}
  .unit-head {{display:flex; align-items:center; justify-content:space-between; gap:0.6rem; flex-wrap:wrap;
      margin:0.9rem 0 0.5rem;}}
  .unit-head b {{font-size:1rem; color:{TEXT};}}
  .rooms {{display:grid; grid-template-columns:repeat(auto-fill, minmax(118px, 1fr)); gap:0.55rem;}}
  .room {{background:#FFF; border:1px solid {BORDER}; border-radius:10px; padding:0.5rem 0.55rem;
      box-shadow:0 1px 2px rgba(0,0,0,.04);}}
  .room.has-free {{border-color:#6EE7B7; box-shadow:0 0 0 1px #A7F3D0;}}
  .room-h {{display:flex; justify-content:space-between; align-items:baseline; font-size:0.78rem;
      color:{MUTED}; margin-bottom:0.35rem;}}
  .room-h b {{color:{TEXT}; font-size:0.86rem;}}
  .beds {{display:flex; gap:0.3rem; flex-wrap:wrap;}}
  .bed {{min-width:1.9rem; height:1.9rem; padding:0 0.3rem; border-radius:6px; display:inline-flex;
      align-items:center; justify-content:center; font-size:0.74rem; font-weight:700; color:#FFF;
      cursor:default;}}
  .bed.free {{background:{EMERALD};}}
  .bed.busy {{background:#EF4444;}}
  .bed.mid {{background:#EA580C;}}
  .bed.long {{background:#7F1D1D; outline:2px dashed {AMBER}; outline-offset:1px;}}
  .unit-beds {{display:flex; gap:0.35rem; flex-wrap:wrap; background:#FFF; border:1px solid {BORDER};
      border-radius:10px; padding:0.6rem;}}
  .unit-beds .bed {{min-width:3.4rem;}}
  .expansion {{font-size:0.78rem; color:{MUTED}; margin-top:0.35rem;}}
  .found {{display:flex; flex-wrap:wrap; gap:0.45rem; margin:0.3rem 0 0.2rem;}}
  .found span {{background:#ECFDF5; border:1px solid #A7F3D0; color:#065F46; border-radius:8px;
      padding:0.35rem 0.6rem; font-size:0.84rem; font-weight:600;}}
</style>
"""


@st.cache_data(ttl=600, show_spinner=False)
def _beds(day_iso: str):
    from datetime import date
    return db.bed_map(ctx.get_conn(), date.fromisoformat(day_iso))


def _bed_html(r, label: str) -> str:
    """Verde libre · rojo ocupada < 10 días · naranja 10 a 15 días · vino con borde más de 15 días."""
    if not r.ocupada:
        return f'<span class="bed free" title="{esc(r.ubicacion)} · Libre">{esc(label)}</span>'
    days = r.dias_estancia if r.dias_estancia == r.dias_estancia else 0
    if days > LONG_STAY_DAYS:
        cls, note = "bed busy long", f"más de {LONG_STAY_DAYS} días · revisar plan de salida"
    elif days >= SHORT_STAY_DAYS:
        cls, note = "bed busy mid", f"entre {SHORT_STAY_DAYS} y {LONG_STAY_DAYS} días"
    else:
        cls, note = "bed busy", f"menos de {SHORT_STAY_DAYS} días"
    tip = f"{r.ubicacion} · Ocupada {fmt_num(days, 0)} días ({note})"
    return f'<span class="{cls}" title="{esc(tip)}">{esc(label)}</span>'


def _unit_header(name: str, phys, virt) -> str:
    free = int((phys["ocupada"] == 0).sum())
    pct = phys["ocupada"].mean() * 100 if len(phys) else 0
    tone = "danger" if pct >= 95 else "warn" if pct >= 85 else "ok"
    chips = [chip(f"{free} libres", "ok" if free else "danger"), chip(f"{fmt_num(pct, 1)} % ocupada", tone)]
    return f'<div class="unit-head"><b>{esc(name)}</b><span>{" ".join(chips)}</span></div>'


def _expansion_line(virt) -> str:
    if virt.empty:
        return ""
    used = int(virt["ocupada"].sum())
    return (f'<div class="expansion">Camas de expansión (virtuales): <b>{used}</b> en uso · '
            f'<b>{len(virt) - used}</b> disponibles para habilitar</div>')


def _render_floor(beds, floor: int, only_free: bool) -> None:
    on_floor = beds[beds["piso"] == floor]
    for unit, group in on_floor.groupby("unidad", sort=True):
        phys = group[group["es_virtual"] == 0]
        virt = beds[(beds["unidad"] == unit) & (beds["es_virtual"] == 1)]
        rooms = []
        for room, rb in phys.groupby("habitacion", sort=True):
            free = int((rb["ocupada"] == 0).sum())
            if only_free and not free:
                continue
            bed_html = "".join(_bed_html(r, r.cama if r.cama != "única" else "•") for r in rb.itertuples())
            rooms.append(f'<div class="room{" has-free" if free else ""}"><div class="room-h"><b>Hab. {esc(room)}</b>'
                         f'<span>{free} libre{"s" if free != 1 else ""}</span></div><div class="beds">{bed_html}</div></div>')
        body = f'<div class="rooms">{"".join(rooms)}</div>' if rooms else \
            '<div class="expansion">Sin habitaciones con camas libres.</div>'
        st.markdown(_unit_header(unit, phys, virt) + body + _expansion_line(virt), unsafe_allow_html=True)


def _render_units(beds, units: list[str], only_free: bool) -> None:
    for unit in units:
        group = beds[beds["unidad"] == unit]
        phys, virt = group[group["es_virtual"] == 0], group[group["es_virtual"] == 1]
        if phys.empty and virt.empty:
            continue
        shown = phys[phys["ocupada"] == 0] if only_free else phys
        bed_html = "".join(_bed_html(r, r.codigo_cama) for r in shown.itertuples())
        body = f'<div class="unit-beds">{bed_html}</div>' if bed_html else \
            '<div class="expansion">Sin camas físicas libres en esta unidad.</div>'
        st.markdown(_unit_header(unit, phys, virt) + body + _expansion_line(virt), unsafe_allow_html=True)


FINDER = {"Adulto": "Adultos", "UCI adulto": "Crítica adultos", "Niño": "Pediátrica",
          "Recién nacido": "Neonatal", "Maternidad": "Materna"}


def _finder(beds) -> None:
    with st.container(border=True):
        st.markdown("**¿Dónde hay una cama libre?**")
        choice = st.pills("Para un paciente", list(FINDER), default="Adulto", key="finder_pop",
                          label_visibility="collapsed")
        pop = FINDER.get(choice or "Adulto")
        pool = beds[(beds["poblacion"] == pop) & (beds["servicio"] != "Urgencias")]
        free = pool[(pool["ocupada"] == 0) & (pool["es_virtual"] == 0)]
        if free.empty:
            virt = pool[(pool["es_virtual"] == 1) & (pool["ocupada"] == 0)]
            st.markdown(chip("No hay camas libres", "danger", "●") + " "
                        + (f"Se pueden habilitar <b>{len(virt)}</b> camas de expansión." if not virt.empty
                           else "Coordinar traslado a otra institución."), unsafe_allow_html=True)
            return
        st.markdown(f'<div class="found">{"".join(f"<span>{esc(r.ubicacion)}</span>" for r in free.head(4).itertuples())}'
                    f'</div>', unsafe_allow_html=True)
        if len(free) > 4:
            st.caption(f"y {len(free) - 4} más")


def page_camas() -> None:
    if not ctx.can("camas.ver"):
        st.error("Tu rol no tiene acceso al mapa de camas.")
        st.stop()
    st.markdown(CSS, unsafe_allow_html=True)
    ref = ctx.ref_date()
    beds = _beds(ref.isoformat())
    inpatient = beds[beds["servicio"] != "Urgencias"]
    phys = inpatient[inpatient["es_virtual"] == 0]
    virt = inpatient[inpatient["es_virtual"] == 1]

    st.markdown("### Camas")
    st.markdown(" ".join([
        chip(f"{int((phys['ocupada'] == 0).sum())} libres", "ok", "●"),
        chip(f"{int(phys['ocupada'].sum())} ocupadas", "neutral", "●"),
        chip(f"{int(virt['ocupada'].sum())} en expansión", "warn", "▲"),
    ]) + f' <span class="muted">· datos del {ref:%d/%m/%Y}</span>', unsafe_allow_html=True)

    _finder(beds)

    legend, toggle = st.columns([3, 1.2], vertical_alignment="center")
    legend.markdown(f'<div class="map-legend"><span><i class="sw" style="background:{EMERALD}"></i>Libre</span>'
                    f'<span><i class="sw" style="background:#EF4444"></i>Menos de {SHORT_STAY_DAYS} días</span>'
                    f'<span><i class="sw" style="background:#EA580C"></i>{SHORT_STAY_DAYS} a {LONG_STAY_DAYS} días</span>'
                    f'<span><i class="sw" style="background:#7F1D1D;outline:2px dashed {AMBER}"></i>'
                    f'Más de {LONG_STAY_DAYS} días</span></div>', unsafe_allow_html=True)
    only_free = toggle.toggle("Solo libres", key="map_only_free")

    floors = sorted(int(f) for f in beds["piso"].dropna().unique())
    others = beds[beds["piso"].isna()]
    critical = sorted(u for u, s in zip(others["unidad"], others["subgrupo_cama"])
                      if any(w in str(s).upper() for w in CRITICAL_WORDS))
    critical = list(dict.fromkeys(critical))
    er = sorted(others.loc[others["servicio"] == "Urgencias", "unidad"].unique())
    rest = sorted(set(others["unidad"]) - set(critical) - set(er))

    labels = [f"Piso {f}" for f in floors] + ["UCI y cuidados", "Otras", "Urgencias"]
    tabs = st.tabs(labels)
    for tab, f in zip(tabs, floors):
        with tab:
            _render_floor(beds, f, only_free)
    with tabs[len(floors)]:
        _render_units(beds, critical, only_free)
    with tabs[len(floors) + 1]:
        _render_units(beds, rest, only_free)
    with tabs[len(floors) + 2]:
        _render_units(beds, er, only_free)

    with st.expander("¿De dónde sale esta información?"):
        st.markdown("- **Piso y habitación** se leen del código de cama del sistema del hospital: H-203C = piso 2, "
                    "habitación 203, cama C. Las unidades sin ese código (UCI, intermedios, observación) se "
                    "muestran por unidad.\n"
                    "- **Estado** de cada cama: censo del día de corte de los datos.\n"
                    f"- **Colores de ocupada:** según los días que lleva el paciente en la cama. Más de "
                    f"{LONG_STAY_DAYS} días: conviene revisar el plan de salida.\n"
                    "- **Camas de expansión:** capacidad adicional que el sistema registra como “virtual”.\n"
                    "- Los datos no traen pasillo ni ala; si el hospital los entrega, se agregan a esta vista.")
