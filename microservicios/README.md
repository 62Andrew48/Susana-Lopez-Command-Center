# Microservicios analíticos · HSLV Command Center

Cuatro servicios Flask independientes que responden a necesidades analíticas y predictivas del
Hospital Susana López de Valencia. Leen `hospital.db` en **solo lectura** y no modifican la app de
Streamlit, la API central ni el `requirements.txt` de la raíz.

| Servicio | Puerto | Variable | Qué predice |
|---|---:|---|---|
| `service_urgencias` | 5001 | `URGENCIAS_URL` | Ingresos diarios por urgencias (opcional: por nivel de triage) |
| `service_quirofanos` | 5002 | `QUIROFANOS_URL` | Cirugías realizadas por día (opcional: por área quirúrgica) |
| `service_farmacia` | 5003 | `FARMACIA_URL` | Demanda diaria real: dispensaciones totales o unidades por ítem |
| `service_consultas` | 5004 | `CONSULTAS_URL` | Ingresos ambulatorios por día o servicios por especialidad |

```
microservicios/
├── __init__.py
├── comun.py                 # núcleo compartido: conexión ro, modelo, privacidad, Excel
├── run_services.py          # orquestador: levanta los 4 servicios
├── verificar_servicios.py   # prueba de contrato contra los servicios en ejecución
├── service_urgencias/   (app.py · model.py · reporte.py · requirements.txt)
├── service_quirofanos/  (ídem)
├── service_farmacia/    (ídem)
└── service_consultas/   (ídem)
```

## Instalación

Con el entorno virtual del proyecto activo y `hospital.db` ya construida (`python database.py`):

```bash
# Git Bash / Linux / macOS
for s in microservicios/service_*/; do pip install -r "$s/requirements.txt"; done
```

```powershell
# PowerShell
Get-ChildItem microservicios\service_*\requirements.txt | ForEach-Object { pip install -r $_.FullName }
```

Las cuatro listas son iguales (Flask, scikit-learn, openpyxl, pandas, numpy); están separadas para que
cada servicio pueda desplegarse solo.

## Ejecución

```bash
python microservicios/run_services.py                     # los cuatro
python microservicios/run_services.py urgencias farmacia  # solo algunos
python microservicios/verificar_servicios.py              # en otra terminal: valida el contrato
```

`Ctrl+C` detiene los cuatro procesos en orden y libera los puertos. También se puede levantar uno solo:
`python microservicios/service_urgencias/app.py`.

Puertos opcionales en el `.env` de la raíz (se acepta URL completa o solo el número):

```dotenv
URGENCIAS_URL=http://127.0.0.1:5001
QUIROFANOS_URL=http://127.0.0.1:5002
FARMACIA_URL=http://127.0.0.1:5003
CONSULTAS_URL=http://127.0.0.1:5004
```

Por defecto los servicios escuchan solo en `127.0.0.1`. Para exponerlos en la red local: `--host 0.0.0.0`.

## Contrato REST (igual en los cuatro)

| Método | Ruta | Respuesta |
|---|---|---|
| `GET` | `/health` | `{"servicio", "estado", "modelo_entrenado", "fecha_referencia"}` |
| `GET` | `/kpis` | Indicadores operativos del dominio |
| `POST` | `/predict` | `{"prediccion", "intervalo", "metricas": {"mae", "rmse", "r2", "mae_baseline", "supera_baseline"}, "features_usadas", ...}` |
| `GET` | `/reporte.xlsx` | Excel con las hojas `Historico clasificado`, `Consumo o uso` y `Prediccion` |

`/predict` acepta un JSON vacío (pronostica el día siguiente al último día completo) o estos campos:

| Servicio | Campos opcionales |
|---|---|
| Todos | `"fecha": "YYYY-MM-DD"` (hasta 14 días adelante) |
| Urgencias | `"nivel_triage": 1..5` |
| Quirófanos | `"area_quirofano": "QUIROFANOS CENTRAL"` (alias: `"sala"`) |
| Farmacia | `"codigo": "..."` o `"tipo_item": "Medicamento" \| "Insumo / dispositivo"` |
| Consultas | `"especialidad": "PEDIATRIA"`, `"incluir_urgencias": true` |

```bash
curl -X POST http://127.0.0.1:5001/predict -H "Content-Type: application/json" -d '{"nivel_triage": 2}'
```

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:5003/predict -ContentType "application/json" -Body '{"tipo_item":"Medicamento"}'
Invoke-WebRequest http://127.0.0.1:5002/reporte.xlsx -OutFile reporte_quirofanos.xlsx
```

Además de los campos del contrato, la respuesta incluye `objetivo`, `fecha_objetivo`, `horizonte_dias`,
el periodo de entrenamiento y prueba, y `advertencias`.

Errores: `400` parámetros inválidos · `404` filtro inexistente · `422` historia insuficiente ·
`503` base no disponible.

## Método de predicción

- **Modelo:** `RandomForestRegressor` (300 árboles, `random_state=42`) sobre variables de calendario
  (día de la semana, festivos de Colombia, día posterior a festivo) y rezagos (1, 7 y 14 días, medias de
  7 y 14 días, promedio del mismo día en las 3 semanas previas). Cada variable del día *t* usa solo datos
  hasta *t-1*.
- **Validación honesta:** partición temporal, sin barajar. Se entrena con el pasado y se evalúa con las
  últimas 4 semanas. No se usa `train_test_split` aleatorio.
- **Línea base ingenua:** el valor del mismo día de la semana anterior. Si el modelo no la supera,
  `supera_baseline` es `false` y la respuesta lo advierte de forma explícita.
- **Intervalo:** conformal al 90 %, calculado con los errores fuera de muestra. Para horizontes mayores
  a 1 día se amplía por √h (aproximación, también advertida).
- **Producción:** tras validar, el modelo se reentrena con todo el histórico. El modelo general se
  entrena en segundo plano al arrancar; los modelos por filtro se entrenan al primer uso y quedan en
  caché hasta que `hospital.db` cambie.

## Decisiones tomadas por los datos

- **"Hoy" = `metadatos.fecha_referencia`** (21/09/2026). No se usa la fecha del sistema.
- **Último día parcial:** si el último día del extracto trae menos de la mitad de lo habitual para ese
  día de la semana, se excluye del entrenamiento y el pronóstico empieza en ese día.
- **Efecto de borde inicial:** el extracto solo trae ingresos desde su inicio. En las primeras semanas
  faltan la medicación y las cirugías de pacientes que ya estaban hospitalizados. Se descartan 14 días
  en farmacia y 7 en quirófanos (`dias_calentamiento` en cada `model.py`).
- **Quirófanos:** `programacion_cirugia` no tiene fecha, sala ni estado. Se usa la tabla derivada
  `cirugias` con `fecha_cirugia IS NOT NULL`. `area_quirofano` sustituye a la sala y no hay horas, así
  que no se calculan tiempos de rotación.
- **Farmacia:** el stock es simulado (`stock_simulado = 1`) y siempre se marca como tal. Se predice la
  demanda, que sí es real. Los agregados se miden en dispensaciones (líneas) porque sumar tabletas y
  mililitros de productos distintos no tiene sentido; por ítem se predicen unidades.
- **Consultas:** no hay citas ni inasistencias. Se modela la demanda ambulatoria atendida y por
  defecto se excluye la vía Urgencias para no contarla dos veces.

## Privacidad (Habeas Data)

Toda la información se agrega en SQL: ningún `SELECT` pide `id_paciente` ni `oid_ingreso`. Además,
cada DataFrame y cada JSON pasan por una verificación que **bloquea la respuesta completa** si aparece
un identificador (falla cerrada).

## `.gitignore`

No reemplaces el archivo actual; solo anexa estas reglas al final.

```bash
# Git Bash
printf '\n# --- Nuevas reglas de microservicios ---\nmicroservicios/**/*.joblib\nmicroservicios/**/*.pkl\nmicroservicios/**/*.xlsx\n*.xlsx\n' >> .gitignore
```

```powershell
# PowerShell
Add-Content .gitignore "`n# --- Nuevas reglas de microservicios ---`nmicroservicios/**/*.joblib`nmicroservicios/**/*.pkl`nmicroservicios/**/*.xlsx`n*.xlsx"
```

`*.xlsx` ignora cualquier Excel del repositorio. Si algún día necesitan versionar una plantilla, pueden
añadir después una excepción como `!docs/plantilla.xlsx`.

## Solución de problemas

| Síntoma | Causa y solución |
|---|---|
| `Puertos ocupados` | Otro proceso usa el puerto. Ciérralo o cambia la variable `*_URL`. |
| `/health` responde 503 | No existe `hospital.db`: ejecuta `python database.py` en la raíz. |
| `modelo_entrenado: false` | Aún está entrenando (unos segundos) o falló; revisa `detalle` en `/health`. |
| `ModuleNotFoundError: config` | Ejecuta los scripts desde el repositorio; cada archivo agrega la raíz a `sys.path`. |
| Los datos no se actualizan | Tras reconstruir `hospital.db` las cachés se invalidan solas en la siguiente petición. |
