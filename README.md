# HSLV · Centro de mando operativo con Agente IA

**Hackatón de Ingeniería de Sistemas · Campus Party FUP 2026**
Reto: Hospital Susana López de Valencia (Popayán, mediana complejidad, ~500 pacientes/día).

## El problema

La información de ocupación de camas, tiempos de espera, quirófanos y farmacia vive en sistemas separados.
Directivos y jefes de servicio dependen de reportes manuales de TI, y las decisiones críticas llegan tarde.

## La solución

Un MVP que unifica el extracto del HIS en una base SQLite y ofrece tres herramientas:

1. **Asistente IA (NL2SQL):** preguntas en español → SQL de solo lectura → respuesta, tabla, gráfico y SQL visible.
   Si el LLM falla o no hay API key, un **Plan B** con SQL validado responde las preguntas clave.
2. **Tablero de KPIs:** ocupación por unidad, espera por triage, causa raíz por turno, diagnósticos,
   rotación de medicamentos, cumplimiento quirúrgico, especialidades y perfil de pacientes.
3. **Alertas y acciones:** sobreocupación con camas a habilitar y personal a reasignar, stock crítico con
   orden de compra, picos de demanda por patología, metas de triage y balance de quirófanos.

---

## Ejecución desde cero

Requisitos: Python 3.10+ y los 7 archivos `.txt` del reto (no se versionan: son datos del hospital).

```bash
git clone https://github.com/62Andrew48/Susana-Lopez-Command-Center.git
cd Susana-Lopez-Command-Center
python -m venv .venv
source .venv/Scripts/activate      # Windows (Git Bash). En Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
# copiar Paciente.txt, Ingresos.txt, Atencion.txt, Triage.txt, Servicios.txt,
# MedicamentoInsumo.txt y ProgramacionCirugia.txt en ./Datos/
streamlit run app.py               # abre http://localhost:8501; hospital.db se construye sola (~20 s)
```

Si faltan archivos en `Datos/`, la app lo dice en pantalla y no arranca a medias.

Opcional:

```bash
cp .env.example .env              # y configura LLM_PROVIDER + API key para activar el LLM
python database.py --rebuild      # reconstruir la base manualmente
uvicorn api:app --port 8000       # API REST -> http://localhost:8000/docs
python agent.py                   # demo por consola de las 4 preguntas
python -m pytest -q               # 121 pruebas (seguridad, intenciones, LLM simulado, ciclo clínico, RBAC, operación, microservicios)
```

Sin `.env` todo funciona en modo **Plan B** (sin red y sin costo).

---

## Versión 3: una pantalla por pregunta

La interfaz se reorganizó para que cada rol vea primero lo que tiene que hacer, no el histórico:

- **Hoy** (inicio): cuatro cifras del día, **"Qué hacer ahora"** con el botón que lo resuelve y la **cola de
  urgencias** a la hora del reloj clínico (códigos `URG-xxxx` no reversibles, sin nombres). El paciente ve sus
  medicamentos por reclamar y su próxima cita.
- **Mapa de camas** por piso, habitación y cama. El piso y la habitación salen del código del HIS
  (`H-203C` = piso 2, habitación 203, cama C). Buscador de un clic: *¿dónde hay cama libre para un adulto, un niño,
  un recién nacido, maternidad o UCI?* Las camas virtuales se muestran como **capacidad de expansión**.
- **Campana de notificaciones** por rol, con enlace a donde se resuelve cada aviso.
- **Asistente flotante** en todas las páginas, con alcance por permisos:

| Rol | Qué puede preguntar |
|---|---|
| Admin y Médico | Toda la base analítica anonimizada (LLM o Plan B), ubicación de camas, glosario; ve el SQL |
| Enfermería | Camas, inventario, rotación, espera en urgencias, alertas y glosario. Sin SQL libre ni LLM |
| Paciente | Solo sus fórmulas y citas, y el glosario. Nunca consulta la base del hospital |

- **Glosario del sector salud** (tomado del glosario entregado con el reto): al pasar el cursor por un término y
  en el chat ("¿qué es triage II?").

## Microservicios predictivos (solo gerencia)

Cuatro servicios Flask independientes en `microservicios/` (urgencias, quirófanos, farmacia y consulta
externa), cada uno con un Random Forest validado con partición temporal contra una línea base ingenua
(detalle en `microservicios/README.md`). La app los consume con `ml_services.py`:

- **Página "Pronósticos"** (Admin): una tarjeta por servicio con la predicción, su rango probable, si mejora o
  no a la línea base y el reporte Excel.
- **Asistente:** una pregunta de pronóstico ("¿cuántos ingresos a urgencias se esperan mañana?") va al
  microservicio del dominio.
- **Fiabilidad:** tiempo máximo de 2 s por llamada y circuit breaker de 30 s. Si un servicio cae, su tarjeta
  dice "no disponible", las demás siguen y el asistente responde con el histórico avisando el motivo.

```bash
for s in microservicios/service_*/; do pip install -r "$s/requirements.txt"; done
python microservicios/run_services.py      # en otra terminal; la app funciona también sin ellos
```

## Versión 2: módulo clínico con roles (RBAC)

La v2 agrega una base transaccional separada (`clinico.db`, esquema en `sql/schema_clinico.sql`) con usuarios,
roles, turnos, historias clínicas, prescripciones, dispensación, citas y un libro mayor de inventario. La interfaz
muestra a cada rol solo sus secciones:

| Sección | Admin | Doctor | Enfermería | Paciente |
|---|:-:|:-:|:-:|:-:|
| Hoy (acciones del turno y cola de urgencias) | ✓ | ✓ | ✓ | ✓ (sus pendientes) |
| Mapa de camas | ✓ | ✓ | ✓ | |
| Indicadores (KPIs, tendencias, camas, urgencias, epidemiología) | ✓ completo | camas y urgencias | camas y urgencias | |
| Asistente IA (página completa con SQL) | ✓ | ✓ | | |
| Asistente flotante | ✓ | ✓ | limitado | solo sus datos |
| Alertas y acciones (semáforo, orden de compra, acciones) | ✓ + CSV | ✓ | ✓ | |
| Clínico y farmacia (historias, prescripción, dispensación) | | ✓ | ✓ (sin prescribir) | |
| Mis fórmulas y citas | | | | ✓ |
| Datos y auditoría (extractos, bitácora) | ✓ | | | |

`clinico.db` se crea y se siembra sola en el primer arranque: 4 usuarios de demostración, turnos diurnos y
6 historias clínicas sobre pacientes e ingresos **reales** del extracto. Las fórmulas y las citas son **sintéticas**.
El reloj de la cabecera (🕒) cambia a turno nocturno o reinicia el escenario sin tocar la base analítica.

### Guion de demostración (≈4 minutos)

1. **Admin** → Hoy: 86,3 % de camas físicas ocupadas y "Hospitalización 2 al 100 %" con la cama libre más cercana
   ya ubicada → **Ver camas** → Piso 1. En la burbuja: *"¿Dónde hay camas libres para pediatría en el piso 4?"*.
2. **Dra. Ruiz** → Clínico y farmacia: la historia del paciente 110 con su línea de tiempo inmutable.
3. **Enf. Gómez** → Dispensación → **"Simular avance de 72 horas"**: 15 dosis de acetaminofén y 10 de enoxaparina
   vuelven a stock. La enoxaparina, de continuidad crítica, genera una alerta en lugar de un bloqueo.
4. **Paciente 110** → la campana avisa "Tu fórmula venció" → Mis fórmulas y citas → "Solicitar cita".
5. **Dra. Ruiz** → Prescripción: "Atender cita" levanta el bloqueo.
6. Reloj → **Turno de noche** → Historias: el acceso queda bloqueado → "Romper el vidrio" con justificación.
7. **Admin** → Datos y auditoría → Bitácora: el acceso de emergencia aparece registrado.

Antes de cada ensayo usa "↺ Reiniciar escenario clínico".

## Arquitectura

```mermaid
flowchart LR
    subgraph Fuente["Extracto HIS (./Datos, '|')"]
        P[Paciente] & I[Ingresos] & A[Atencion] & T[Triage]
        S[Servicios] & M[MedicamentoInsumo] & C[ProgramacionCirugia]
    end
    subgraph ETL["database.py (Modelo)"]
        L[Lectura QUOTE_NONE] --> N[Limpieza y fechas] --> AN[Anonimización] --> D[Tablas derivadas]
    end
    DB[(hospital.db<br/>SQLite indexada)]
    subgraph Agente["agent.py (Controlador)"]
        R{Router} -->|LLM| G[Prompt few-shot + DDL] --> SG[SQLGuard]
        R -->|Plan B| PB[Intenciones regex] --> SG
        SG --> EX[Ejecutor solo-lectura<br/>+ authorizer]
        EX --> RE[Motor de recomendaciones]
    end
    UI["app.py · Streamlit (Vista)<br/>Tablero · Chat · Alertas"]
    API["api.py · FastAPI<br/>/api/query · /api/kpis · /api/alerts"]
    LLM[[OpenAI · Anthropic · Ollama/SQLCoder]]

    Fuente --> ETL --> DB
    DB --> EX
    G <--> LLM
    UI --> Agente
    API --> Agente
    UI --> DB
```

**Secuencia de una pregunta**

```mermaid
sequenceDiagram
    actor U as Jefe de servicio
    participant UI as Streamlit
    participant AG as HospitalAgent
    participant LLM as LLM
    participant G as SQLGuard
    participant DB as SQLite (ro)
    U->>UI: ¿Cuántas camas de UCI están ocupadas hoy?
    UI->>AG: ask(pregunta)
    AG->>LLM: esquema + reglas + ejemplos + pregunta
    LLM-->>AG: SELECT ...
    AG->>G: sanitize(sql)
    G-->>AG: SQL seguro + LIMIT
    AG->>DB: ejecutar (authorizer: solo lectura)
    DB-->>AG: filas (sin identificadores)
    AG->>LLM: redactar respuesta con el resultado
    AG-->>UI: respuesta + SQL + tabla + gráfico + alertas
    Note over AG,LLM: Si el LLM falla → Plan B con SQL validado
```

| Módulo | Rol MVC | Qué hace |
|---|---|---|
| `config.py` | — | Variables de `.env`, rutas y umbrales de negocio |
| `database.py` | Modelo | ETL, 12 tablas indexadas, funciones de KPI reutilizables |
| `agent.py` | Controlador | NL2SQL, `SQLGuard`, ejecutor seguro, Plan B, `RecommendationEngine`, Factory de LLM |
| `app.py` + `ui/` | Vista | Cabecera, menú por permisos y páginas: `pages_hoy`, `pages_camas`, `pages_analytics`, `pages_clinical` |
| `ui/assistant_scope.py` | Controlador | Qué puede responder el asistente según los permisos del rol |
| `ui/notifications.py` | Servicio | Notificaciones por rol (lógica pura, probada) |
| `ui/glossary.py` | — | Términos del sector salud en lenguaje sencillo |
| `pharmacy_service.py` | Servicio | Ciclo de prescripción, caducidad con retorno a stock y autorización RBAC |
| `demo_seed.py` | — | Escenario de demostración y reloj clínico |
| `api.py` | Servicio | Endpoints REST del reto |
| `ml_services.py` | Servicio | Cliente de los microservicios con tiempo máximo y circuit breaker |
| `tests/` | — | 121 pruebas: seguridad SQL, intenciones, LLM simulado, ciclo clínico, RBAC, camas, cola, notificaciones y alcance del asistente |

## Modelo de datos (`hospital.db`)

| Tabla | Filas | Contenido |
|---|---:|---|
| `pacientes` | 14.502 | Anonimizada: sexo, edad, grupo etario, régimen, asegurador, municipio, zona |
| `ingresos` | 17.781 | Episodio + servicio normalizado, capítulo CIE-10, espera, triage, turno, estancia estimada |
| `atenciones` | 17.375 | Primera atención médica |
| `triage` | 16.106 | Clasificación y signos vitales (sin motivo de consulta) |
| `servicios` | 582.357 | Procedimientos CUPS, área y especialidad |
| `medicamentos_insumos` | 579.465 | Dispensación + tipo de ítem |
| `inventario_farmacia` | 1.327 | Consumo 30 días, rotación, stock, días de inventario |
| `ocupacion_diaria` | 2.736 | Censo diario por unidad: capacidad, ocupadas, % |
| `camas` | 712 | Catálogo de camas observadas (capacidad) |
| `cirugias` | 6.156 | Programación consolidada y estado (realizada / sin evidencia) |

## Decisiones tomadas a partir de los datos

Estas decisiones salieron de perfilar el extracto antes de programar; conviene mencionarlas ante el jurado.

- **"Hoy" = 21/09/2026**, la última fecha de ingreso del extracto (configurable con `REFERENCE_DATE`).
  Usar la fecha del sistema devolvería cero en todas las preguntas "de hoy".
- **Triage.txt trae comillas sin cerrar** en `MotivoConsulta`: el lector estándar de pandas descarta cientos de
  filas en silencio. Se lee con `quoting=QUOTE_NONE` y no se pierde ninguna.
- **No hay fecha de egreso.** La estancia se estima desde la hospitalización hasta la última prestación
  (servicio o medicamento) registrada.
- **`CodigoCama` es la última cama del episodio.** Un paciente que pasó por UCI y terminó en hospitalización no
  figura como UCI. Consecuencias: la foto del día es confiable (los pacientes que hoy están en UCI sí aparecen),
  pero la serie histórica subestima UCI e intermedios. Por eso el tablero añade los **días-cama facturados**
  (CUPS de internación) y los picos de demanda se calculan por **capítulo CIE-10**, no por servicio: por servicio,
  UCI aparentaba un falso +187 %.
- **No hay existencias de farmacia.** El consumo diario es real; el stock se simula de forma determinística y
  queda marcado (`stock_simulado = 1`). Si farmacia entrega `Datos/Inventario.txt` (`CodigoServicio|Stock`),
  el cálculo pasa a ser real sin tocar código.
- **Camas físicas y virtuales.** El HIS registra 223 camas "virtuales" fuera de Urgencias (capacidad de
  expansión). La página Hoy, el mapa y la tendencia calculan la ocupación sobre las **300 camas físicas**
  (86,3 % el 21/09) y muestran aparte los pacientes en camas de expansión (108).
- **Ubicación física.** El código de cama trae piso y habitación en hospitalización y gineco-obstetricia
  (`H-203C`, `G-108B`); las demás unidades solo traen unidad y número. Los datos no traen pasillo ni ala.
- **Cola de urgencias.** Un paciente está "esperando" a la hora `t` si ingresó por urgencias antes de `t` y su
  primera atención (`FechaAtencion`) es posterior a `t`. El extracto llega hasta el 21/09 a las 14:33.
- **Triage:** 17.781 filas en el extracto, 16.106 con `OidTriage`; las 1.675 restantes no tienen identificador
  (el diccionario de datos indica que acepta nulos), no son filas perdidas.
- **Programación quirúrgica sin fecha.** Según el diccionario, `ProgramacionCirugia` solo trae consecutivo,
  paciente, ingreso y código de servicio. La fecha y el quirófano se derivan de los servicios prestados en
  áreas de QUIRÓFANOS del mismo ingreso.
- **Cirugías:** solo 1.345 de 6.156 programaciones tienen su ingreso dentro del extracto; el cumplimiento se
  calcula sobre ellas (97 %).

## Seguridad

- **SQL:** solo `SELECT`/`WITH`, una sentencia, lista negra (`DROP`, `DELETE`, `UPDATE`, `INSERT`, `PRAGMA`,
  `ATTACH`…), sin tablas `sqlite_*`, comentarios eliminados, `LIMIT` forzado.
- **Defensa en profundidad:** conexión `mode=ro` + `set_authorizer` de SQLite que niega cualquier escritura
  aunque el filtro fuera evadido + tiempo máximo por consulta.
- **Privacidad:** en la ingesta se eliminan nombre, fecha de nacimiento y motivo de consulta; las respuestas
  descartan identificadores (`id_paciente`, `oid_ingreso`…). Un SQL malicioso del LLM se bloquea y se reporta.
- **Credenciales:** solo en `.env` (ignorado por git, igual que los datos del hospital y `hospital.db`).

## API

| Método | Ruta | Descripción |
|---|---|---|
| `POST` | `/api/query` | `{"question": "..."}` → respuesta, SQL, filas, gráfico sugerido, recomendaciones |
| `GET` | `/api/kpis?start=&end=` | KPIs precalculados (por defecto, mes en curso) |
| `GET` | `/api/alerts` | Alertas priorizadas con acción recomendada |
| `GET` | `/health` | Estado, fecha de referencia y motor activo |

---

## Guía para el pitch (7 minutos)

**1. Problema (45 s).** "Cada mañana un jefe de servicio pide a TI un reporte que llega tarde. Mientras tanto,
hoy la hospitalización 2 está al 100 %." Mostrar la pestaña de alertas.

**2. Solución (30 s).** Un asistente que responde en segundos con datos, gráfico y acción recomendada.

**3. Demo con las 4 preguntas (3 min).** Usar los botones del asistente y abrir el SQL de al menos una:

| Pregunta | Respuesta con los datos del reto |
|---|---|
| ¿Cuántas camas de UCI están ocupadas hoy? | 30 de 46 (65,2 %); la UCI neonatal está al 83,3 % |
| ¿Medicamentos con menos de 5 días de inventario? | 47 medicamentos (stock simulado, consumo real); el más crítico, cloruro de sodio 20 mEq, con 1 día |
| ¿Espera promedio en urgencias la última semana? | 1 h 0 min en 796 atenciones; Triage 2 espera 46 min frente a la meta de 30 |
| ¿Qué servicio tiene más pacientes este mes? | Urgencias, con 1.093 pacientes (44,4 %); le sigue Pediatría con 405 |

Cierre de la demo: cambiar el motor a "Solo reglas (Plan B)" y repetir una pregunta para mostrar que la demo
no depende de internet. Luego escribir `DROP TABLE ingresos` (o "borra la tabla de ingresos") para mostrar el
bloqueo de seguridad.

**4. Valor añadido (1 min).** Causa raíz (el turno de la tarde concentra la mayor espera, con 73 % de Triage 3),
alerta temprana (respiratorio +18 % → revisar antibióticos), orden de compra descargable y balance de
quirófanos (viernes 92 cirugías frente a 54 los lunes).

**5. Arquitectura y tecnologías (45 s).** Diagrama de este README. Streamlit (tablero y chat en Python puro),
FastAPI (integración con el HIS), SQLite (cero instalación), LLM intercambiable por Factory.

**6. Limitaciones y mejoras (45 s).** Ver la sección siguiente. Terminar con la mejora 1: modelo local.

**Preguntas probables del jurado**

- *¿Y si el LLM inventa un SQL peligroso?* Tres barreras: filtro, conexión de solo lectura y authorizer del motor.
- *¿Por qué la ocupación histórica de UCI es baja?* Explicar el sesgo de "última cama" y la serie de días-cama.
- *¿El stock es real?* No; está marcado como simulado y se reemplaza con un archivo de farmacia.
- *¿Por qué SQLite y no PostgreSQL?* Es la opción recomendada por el reto: sin servidor, la demo arranca en
  cualquier equipo. El acceso a datos está aislado en `database.py`; migrar es cambiar la conexión.
- *El modelo sugerido traía fecha de salida, médico asignado y fecha de vencimiento, ¿dónde están?* No existen
  en el extracto entregado. Por eso la estancia se estima y el stock se simula, y así se declara en pantalla.
- *¿Hay login?* Hay control de acceso por roles y permisos en base de datos (con auditoría); el ingreso es un
  selector de usuarios de demostración. El esquema ya guarda la contraseña con hash, falta la pantalla de login.

## Limitaciones y mejoras futuras

| Limitación | Mejora |
|---|---|
| Dependencia de API externa y envío de preguntas fuera del hospital | **Modelo local** (Ollama + SQLCoder) ya soportado con `LLM_PROVIDER=ollama` |
| Extracto histórico sin egresos ni traslados de cama | Integración en tiempo real con la Historia Clínica Electrónica (HL7 FHIR) |
| Stock simulado | Conector al inventario de farmacia |
| Alertas por reglas y umbrales | **Machine learning** (Prophet / gradient boosting) para pronosticar ingresos y consumo por patología |
| Ingreso con selector de usuarios de demostración (el RBAC y la auditoría sí son reales) | Pantalla de login con contraseña (hash ya en el esquema) y sesión con JWT |

## Tecnologías

Python 3.10+, pandas, SQLite, Streamlit, Plotly, FastAPI, Uvicorn, Pydantic, Requests, python-dotenv, pytest.
LLM opcional: OpenAI, Anthropic u Ollama.

## Equipo

| Integrante | Rol |
|---|---|
| Sebastián Moncayo Ordoñez | Dirección del proyecto, experiencia por rol (Hoy, mapa de camas, notificaciones, asistente) |
| _(nombre)_ | _(rol)_ |
| _(nombre)_ | _(rol)_ |
