"""
ui/pages_inventario.py — Inventario de farmacia (permiso inventario.auditar: gerencia).

El stock no se edita: cada cambio es un movimiento del libro mayor (inventario_movimientos) con usuario y hora.
  * Existencias: lo que queda, uso diario y cuánto se recomienda pedir.
  * Llegada de pedido: suma unidades (ENTRADA_COMPRA). Se puede recibir de una vez la orden de los urgentes.
  * Conteo físico: si lo contado no coincide con el sistema, se registra un AJUSTE con su motivo.
  * Movimientos: el historial completo (reservas, entregas, devoluciones por caducidad, compras, ajustes).
"""
from __future__ import annotations

import sqlite3

import pandas as pd
import streamlit as st

import pharmacy_service as ps
from agent import fmt_num
from ui import context as ctx
from ui.theme import chip

STATE = {"ROJO": "Urgente", "AMARILLO": "Pronto", "VERDE": "Suficiente", "SIN_CONSUMO": "Sin uso"}
MOVE = {"SALDO_INICIAL": "Saldo inicial", "ENTRADA_COMPRA": "Llegada de pedido", "RESERVA": "Apartado para paciente",
        "DISPENSACION": "Entrega al paciente", "LIBERACION_RESERVA": "Devolución a stock", "AJUSTE": "Ajuste por conteo",
        "BAJA_VENCIMIENTO": "Baja por vencimiento"}


def page_inventario() -> None:
    if not ctx.can("inventario.auditar"):
        st.error("Tu rol no tiene acceso al inventario.")
        st.stop()
    clin = ctx.get_clin()
    inv = pd.DataFrame([dict(r) for r in ps.inventory(clin)])
    red = inv[inv["semaforo"] == "ROJO"]
    st.markdown("### Inventario de farmacia")
    st.markdown(" ".join([chip(f"{len(inv)} ítems", "neutral"), chip(f"{len(red)} urgentes", "danger", "●"),
                          chip(f"{int((inv['semaforo'] == 'AMARILLO').sum())} para pedir pronto", "warn", "●"),
                          chip("Existencias iniciales simuladas · cada movimiento es real", "neutral")]),
                unsafe_allow_html=True)

    tab_stock, tab_held, tab_orders, tab_in, tab_count, tab_moves = st.tabs(
        ["Existencias", "Apartados para pacientes", "Pedidos a proveedor", "Llegada de pedido", "Conteo físico",
         "Movimientos"])
    with tab_orders:
        waiting = ps.backorders(clin)
        if waiting:
            st.warning(f"{len(waiting)} fórmula(s) esperan existencias. Lo que pidas aquí define la fecha que se le "
                       "informa al paciente; al registrar la llegada se apartan solas.")
            st.dataframe(pd.DataFrame([{"Ítem": w["producto"].capitalize(), "Paciente": w["paciente"],
                                        "Unidades": w["dosis_prescritas"], "Formulada": w["fecha_prescripcion"][:16],
                                        "Llegada estimada": w["llegada_estimada"] or "Sin pedido"} for w in waiting]),
                         hide_index=True, width="stretch", height=min(38 * (len(waiting) + 1), 220))
        with st.form("inv_order"):
            names = dict(zip(inv["codigo"], inv["nombre"].str.capitalize()))
            prefer = [w["codigo_producto"] for w in waiting] + list(red["codigo"])
            codes = list(dict.fromkeys(prefer + list(names)))
            code = st.selectbox("Ítem", codes, format_func=lambda c: names[c], index=None,
                                placeholder="Escribe para buscar (primero los que esperan pacientes y los urgentes)")
            c1, c2, c3 = st.columns(3)
            qty = c1.number_input("Unidades pedidas", min_value=1, value=50, step=1)
            from datetime import date, timedelta
            today = date.fromisoformat(ctx.clock()[:10])
            eta = c2.date_input("Llegada estimada", value=today + timedelta(days=3), min_value=today, format="DD/MM/YYYY")
            supplier = c3.text_input("Proveedor")
            if st.form_submit_button("Registrar pedido", type="primary", icon=":material/local_shipping:") and code:
                try:
                    ps.create_order(clin, code, int(qty), eta.isoformat(), ctx.user_id(), supplier, ctx.clock())
                    st.toast(f"Pedido registrado: llega el {eta:%d/%m/%Y}", icon=":material/local_shipping:")
                    st.rerun()
                except sqlite3.IntegrityError as exc:
                    st.error(str(exc))
        orders = ps.open_orders(clin)
        if orders:
            st.dataframe(pd.DataFrame([{"Ítem": o["producto"].capitalize(), "Unidades": o["cantidad"],
                                        "Proveedor": o["proveedor"] or "—", "Pedido": o["fecha_pedido"][:10],
                                        "Llegada estimada": o["fecha_estimada_llegada"],
                                        "Pacientes esperando": o["pacientes_esperando"]} for o in orders]),
                         hide_index=True, width="stretch")
        else:
            st.caption("No hay pedidos en camino.")
    with tab_held:
        held = ps.reservations(clin)
        st.caption(f"Unidades formuladas que esperan a que el paciente las reclame. No están en “Quedan”: están "
                   f"separadas para ese paciente hasta {ps.RESERVE_DAYS} días; si no las reclama, vuelven solas.")
        if not held:
            st.info("No hay unidades apartadas.")
        else:
            st.dataframe(pd.DataFrame([{"Ítem": h["producto"].capitalize(), "Paciente": h["paciente"],
                                        "Apartadas": h["apartadas"], "Apartado hasta": h["fecha_limite_reclamo"][:16],
                                        "Médico": h["medico"]} for h in held]),
                         hide_index=True, width="stretch", height=min(38 * (len(held) + 1), 430))
    with tab_stock:
        q = st.text_input("Buscar", placeholder="Nombre del medicamento o insumo", key="inv_q")
        only = st.segmented_control("Mostrar", ["Todos", "Urgente", "Pronto"], default="Todos", key="inv_only")
        view = inv
        if q:
            view = view[view["nombre"].str.contains(q, case=False, na=False)]
        if only in ("Urgente", "Pronto"):
            view = view[view["semaforo"] == ("ROJO" if only == "Urgente" else "AMARILLO")]
        table = view.assign(estado=view["semaforo"].map(STATE), nombre=view["nombre"].str.capitalize())[
            ["nombre", "tipo_item", "estado", "disponible", "reservado", "consumo_diario_promedio",
             "orden_sugerida_15d"]]
        st.dataframe(table, hide_index=True, width="stretch", height=430, column_config={
            "nombre": st.column_config.TextColumn("Ítem", width="medium"), "tipo_item": None, "estado": "Estado",
            "disponible": st.column_config.NumberColumn("Quedan", format="%d"),
            "reservado": st.column_config.NumberColumn("Apartadas para pacientes", format="%d"),
            "consumo_diario_promedio": st.column_config.NumberColumn("Uso por día", format="%.1f"),
            "orden_sugerida_15d": st.column_config.NumberColumn("Recomendado pedir", format="%d")})

    with tab_in:
        st.caption("Registra lo que llegó del proveedor. Las unidades quedan disponibles de inmediato.")
        if not red.empty:
            total = int(red["orden_sugerida_15d"].sum())
            c1, c2 = st.columns([3, 1.3], vertical_alignment="center")
            c1.markdown(f"**Orden de los {len(red)} ítems urgentes:** {fmt_num(total)} unidades en total.")
            if c2.button("Recibir orden completa", type="primary", width="stretch", key="inv_bulk"):
                for r in red.itertuples():
                    if r.orden_sugerida_15d > 0:
                        ps.receive_stock(clin, r.codigo, int(r.orden_sugerida_15d), ctx.user_id(),
                                         "Llegada de la orden de compra de urgentes", ctx.clock())
                st.toast(f"Recibidos {len(red)} ítems", icon=":material/local_shipping:")
                st.rerun()
            st.divider()
        with st.form("inv_receive"):
            names = dict(zip(inv["codigo"], inv["nombre"].str.capitalize()))
            code = st.selectbox("Ítem", list(names), format_func=lambda c: names[c], index=None,
                                placeholder="Escribe para buscar…")
            suggested = int(inv.loc[inv["codigo"] == code, "orden_sugerida_15d"].iat[0]) if code else 0
            qty = st.number_input("Unidades recibidas", min_value=1, value=max(suggested, 1), step=1)
            note = st.text_input("Proveedor o número de factura (opcional)")
            if st.form_submit_button("Registrar llegada", type="primary") and code:
                done = ps.receive_stock(clin, code, int(qty), ctx.user_id(), note, ctx.clock())
                st.toast(f"Sumadas {int(qty)} und. a {names[code]}"
                         + (f" · {len(done)} fórmula(s) en espera quedaron apartadas" if done else ""),
                         icon=":material/check_circle:")
                st.rerun()

    with tab_count:
        st.caption("Si lo que cuentas en la estantería no coincide con el sistema, registra el conteo: "
                   "se corrige con un ajuste y queda quién lo hizo y por qué.")
        with st.form("inv_count"):
            names = dict(zip(inv["codigo"], inv["nombre"].str.capitalize()))
            code = st.selectbox("Ítem contado", list(names), format_func=lambda c: names[c], index=None,
                                placeholder="Escribe para buscar…", key="inv_count_item")
            counted = st.number_input("Unidades contadas", min_value=0, step=1)
            reason = st.text_input("Motivo", placeholder="Conteo semanal, rotura, vencimiento…")
            if st.form_submit_button("Registrar conteo") and code:
                try:
                    delta = ps.adjust_stock(clin, code, int(counted), ctx.user_id(), reason, ctx.clock())
                    st.toast("Sin diferencias" if not delta else f"Ajuste de {delta:+d} und.", icon=":material/fact_check:")
                    st.rerun()
                except sqlite3.IntegrityError as exc:
                    st.error(str(exc))

    with tab_moves:
        moves = pd.DataFrame([dict(r) for r in ps.movements(clin, limit=300)])
        if moves.empty:
            st.info("Sin movimientos.")
        else:
            moves["tipo"] = moves["tipo"].map(MOVE).fillna(moves["tipo"])
            moves["producto"] = moves["producto"].str.capitalize()
            st.dataframe(moves, hide_index=True, width="stretch", height=430, column_config={
                "fecha": "Fecha", "producto": "Ítem", "tipo": "Movimiento",
                "delta_disponible": st.column_config.NumberColumn("Cambio en disponibles", format="%+d"),
                "delta_reservado": st.column_config.NumberColumn("Cambio en apartadas", format="%+d"),
                "usuario": "Quién", "nota": "Nota"})
            st.caption("Los movimientos no se editan ni se borran (regla de la base de datos): un error se corrige "
                       "con un ajuste nuevo.")
