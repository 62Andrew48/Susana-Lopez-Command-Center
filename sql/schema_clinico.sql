-- =============================================================================
-- schema_clinico.sql — Base TRANSACCIONAL del HSLV (clinico.db) · SQLite 3.35+
--
-- Decisión de arquitectura: esta base es SEPARADA de hospital.db (analítica).
--   * hospital.db se reconstruye desde los extractos del HIS (DROP + CREATE): si
--     las historias clínicas vivieran allí, cada recarga las borraría.
--   * El agente NL2SQL se conecta SOLO a hospital.db: físicamente no puede leer
--     historias clínicas, prescripciones ni usuarios.
-- Por eso id_paciente se guarda como referencia lógica (validada por la API) y no
-- como FOREIGN KEY entre archivos, algo que SQLite no soporta.
-- =============================================================================
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;          -- lecturas concurrentes mientras se escribe

-- -----------------------------------------------------------------------------
-- 1. Seguridad: roles, permisos, usuarios, turnos y auditoría
-- -----------------------------------------------------------------------------
CREATE TABLE roles (
    id          INTEGER PRIMARY KEY,
    codigo      TEXT NOT NULL UNIQUE CHECK (codigo IN ('ADMIN','DOCTOR','ENFERMERIA','PACIENTE','FACTURACION','QUIROFANOS')),
    nombre      TEXT NOT NULL
);

CREATE TABLE permisos (
    id          INTEGER PRIMARY KEY,
    codigo      TEXT NOT NULL UNIQUE,          -- recurso.accion, p. ej. 'prescripcion.crear'
    descripcion TEXT NOT NULL
);

CREATE TABLE rol_permisos (
    rol_id      INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permiso_id  INTEGER NOT NULL REFERENCES permisos(id) ON DELETE CASCADE,
    PRIMARY KEY (rol_id, permiso_id)
);

CREATE TABLE usuarios (
    id                    INTEGER PRIMARY KEY,
    usuario               TEXT NOT NULL UNIQUE,
    hash_password         TEXT NOT NULL,                 -- bcrypt/argon2, nunca texto plano
    nombre_mostrado       TEXT NOT NULL,
    rol_id                INTEGER NOT NULL REFERENCES roles(id),
    -- Estado de la CUENTA (¿puede autenticarse?). La disponibilidad asistencial va en `turnos`.
    estado_cuenta         TEXT NOT NULL DEFAULT 'PENDIENTE_ACTIVACION'
                          CHECK (estado_cuenta IN ('PENDIENTE_ACTIVACION','ACTIVO','SUSPENDIDO','INACTIVO')),
    motivo_estado         TEXT,
    registro_profesional  TEXT,                          -- ReTHUS (obligatorio para DOCTOR/ENFERMERIA)
    especialidad          TEXT,
    id_paciente           INTEGER,                       -- solo rol PACIENTE (ref. lógica a pacientes)
    intentos_fallidos     INTEGER NOT NULL DEFAULT 0,
    bloqueado_hasta       TEXT,
    creado_en             TEXT NOT NULL DEFAULT (datetime('now')),
    ultimo_acceso         TEXT,
    correo                TEXT,                          -- para recuperar la contraseña
    preferencia_tema      TEXT NOT NULL DEFAULT 'claro' CHECK (preferencia_tema IN ('claro','oscuro')),
    debe_cambiar_clave    INTEGER NOT NULL DEFAULT 0 CHECK (debe_cambiar_clave IN (0,1))
);
CREATE UNIQUE INDEX ux_usuario_paciente ON usuarios(id_paciente) WHERE id_paciente IS NOT NULL;

-- Coherencia rol ↔ datos obligatorios (CHECK no puede consultar otra tabla; se usa trigger)
CREATE TRIGGER trg_usuario_coherencia_ins BEFORE INSERT ON usuarios
BEGIN
    SELECT CASE
      WHEN (SELECT codigo FROM roles WHERE id = NEW.rol_id) = 'PACIENTE' AND NEW.id_paciente IS NULL
        THEN RAISE(ABORT, 'Un usuario PACIENTE debe estar vinculado a id_paciente')
      WHEN (SELECT codigo FROM roles WHERE id = NEW.rol_id) <> 'PACIENTE' AND NEW.id_paciente IS NOT NULL
        THEN RAISE(ABORT, 'Solo el rol PACIENTE puede vincular id_paciente')
      WHEN (SELECT codigo FROM roles WHERE id = NEW.rol_id) IN ('DOCTOR','ENFERMERIA')
           AND NEW.registro_profesional IS NULL
        THEN RAISE(ABORT, 'DOCTOR y ENFERMERIA requieren registro profesional (ReTHUS)')
    END;
END;

-- Disponibilidad asistencial: TURNO (presencial) o GUARDIA (localizable). Ortogonal al estado de cuenta.
CREATE TABLE turnos (
    id          INTEGER PRIMARY KEY,
    usuario_id  INTEGER NOT NULL REFERENCES usuarios(id),
    servicio    TEXT NOT NULL,                           -- mismo vocabulario que ingresos.servicio
    tipo        TEXT NOT NULL CHECK (tipo IN ('TURNO','GUARDIA')),
    inicio      TEXT NOT NULL,
    fin         TEXT NOT NULL,
    CHECK (fin > inicio)
);
CREATE INDEX ix_turnos_ventana ON turnos(usuario_id, inicio, fin);

-- Bitácora inmutable (append-only): quién vio qué historia, cuándo y por qué
CREATE TABLE auditoria_accesos (
    id                     INTEGER PRIMARY KEY,
    fecha                  TEXT NOT NULL DEFAULT (datetime('now')),
    usuario_id             INTEGER REFERENCES usuarios(id),
    accion                 TEXT NOT NULL,                -- LOGIN_OK, LOGIN_FALLIDO, VER_HC, PRESCRIBIR, ...
    recurso                TEXT,
    id_paciente            INTEGER,
    acceso_emergencia      INTEGER NOT NULL DEFAULT 0 CHECK (acceso_emergencia IN (0,1)),  -- "romper el vidrio"
    justificacion          TEXT,
    ip                     TEXT,
    CHECK (acceso_emergencia = 0 OR justificacion IS NOT NULL)
);
CREATE INDEX ix_auditoria_paciente ON auditoria_accesos(id_paciente, fecha);
CREATE TRIGGER trg_auditoria_no_update BEFORE UPDATE ON auditoria_accesos
BEGIN SELECT RAISE(ABORT, 'La auditoría es inmutable'); END;
CREATE TRIGGER trg_auditoria_no_delete BEFORE DELETE ON auditoria_accesos
BEGIN SELECT RAISE(ABORT, 'La auditoría es inmutable'); END;

-- -----------------------------------------------------------------------------
-- 2. Historia clínica, citas y catálogo de farmacia
-- -----------------------------------------------------------------------------
CREATE TABLE historias_clinicas (
    id           INTEGER PRIMARY KEY,
    id_paciente  INTEGER NOT NULL UNIQUE,                -- una HC por paciente (Res. 1995 de 1999)
    creada_en    TEXT NOT NULL DEFAULT (datetime('now')),
    estado       TEXT NOT NULL DEFAULT 'ACTIVA' CHECK (estado IN ('ACTIVA','CERRADA'))
);

-- Eventos de la HC: nunca se editan, se agregan (trazabilidad clínica y legal)
CREATE TABLE historia_clinica_eventos (
    id             INTEGER PRIMARY KEY,
    historia_id    INTEGER NOT NULL REFERENCES historias_clinicas(id),
    oid_ingreso    INTEGER,                              -- episodio del HIS (ref. lógica)
    tipo           TEXT NOT NULL CHECK (tipo IN ('NOTA_EVOLUCION','PRESCRIPCION','DISPENSACION',
                     'ADMINISTRACION_DOSIS','FORMULA_CADUCADA','INTERCONSULTA','CITA','ALERTA',
                     'REGISTRO_HC','ADJUNTO','DATOS_PACIENTE','CIRUGIA')),
    descripcion    TEXT NOT NULL,
    autor_id       INTEGER REFERENCES usuarios(id),      -- NULL = proceso automático del sistema
    fecha          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_hc_eventos ON historia_clinica_eventos(historia_id, fecha);
CREATE TRIGGER trg_hc_eventos_no_update BEFORE UPDATE ON historia_clinica_eventos
BEGIN SELECT RAISE(ABORT, 'Los eventos de la historia clínica no se modifican; registre uno nuevo'); END;
CREATE TRIGGER trg_hc_eventos_no_delete BEFORE DELETE ON historia_clinica_eventos
BEGIN SELECT RAISE(ABORT, 'Los eventos de la historia clínica no se eliminan'); END;

CREATE TABLE citas (
    id                      INTEGER PRIMARY KEY,
    id_paciente             INTEGER NOT NULL,
    medico_id               INTEGER REFERENCES usuarios(id),
    especialidad            TEXT NOT NULL,
    fecha_hora              TEXT NOT NULL,
    motivo                  TEXT NOT NULL CHECK (motivo IN ('PRIMERA_VEZ','CONTROL','REEVALUACION_FORMULA',
                                                            'INTERCONSULTA')),
    prescripcion_origen_id  INTEGER REFERENCES prescripciones(id),
    estado                  TEXT NOT NULL DEFAULT 'PROGRAMADA'
                            CHECK (estado IN ('PROGRAMADA','CUMPLIDA','CANCELADA','NO_ASISTIO')),
    creada_en               TEXT NOT NULL DEFAULT (datetime('now')),
    nota                    TEXT,                           -- motivo que escribe el paciente o admisiones
    creada_por              INTEGER REFERENCES usuarios(id),
    motivo_cancelacion      TEXT
);
CREATE INDEX ix_citas_paciente ON citas(id_paciente, fecha_hora);
-- Un médico no puede tener dos citas programadas a la misma hora (nada de doble agenda)
CREATE UNIQUE INDEX ux_citas_medico_hora ON citas(medico_id, fecha_hora)
    WHERE estado = 'PROGRAMADA' AND medico_id IS NOT NULL;

-- Catálogo sincronizado desde hospital.db (inventario_farmacia): el consumo histórico es real
CREATE TABLE productos_farmacia (
    codigo                   TEXT PRIMARY KEY,
    nombre                   TEXT NOT NULL,
    tipo_item                TEXT NOT NULL,
    consumo_diario_promedio  REAL NOT NULL DEFAULT 0 CHECK (consumo_diario_promedio >= 0),
    critico_continuidad      INTEGER NOT NULL DEFAULT 0 CHECK (critico_continuidad IN (0,1))
    -- 1 = no puede suspenderse sin riesgo (insulina, anticoagulantes, antiepilépticos, ARV...)
);

-- -----------------------------------------------------------------------------
-- 3. Prescripción → reserva → dispensación → (caducidad y retorno a stock)
-- -----------------------------------------------------------------------------
CREATE TABLE prescripciones (
    id                     INTEGER PRIMARY KEY,
    historia_id            INTEGER NOT NULL REFERENCES historias_clinicas(id),
    id_paciente            INTEGER NOT NULL,
    oid_ingreso            INTEGER,
    medico_id              INTEGER NOT NULL REFERENCES usuarios(id),   -- médico tratante que formula
    codigo_producto        TEXT NOT NULL REFERENCES productos_farmacia(codigo),
    dosis                  TEXT NOT NULL,                -- "500 mg vía oral"
    frecuencia_horas       INTEGER NOT NULL CHECK (frecuencia_horas BETWEEN 1 AND 168),
    duracion_dias          INTEGER NOT NULL CHECK (duracion_dias BETWEEN 1 AND 365),
    dosis_prescritas       INTEGER NOT NULL CHECK (dosis_prescritas > 0),
    dosis_entregadas       INTEGER NOT NULL DEFAULT 0,
    ambito                 TEXT NOT NULL CHECK (ambito IN ('AMBULATORIA','HOSPITALARIA')),
    fecha_prescripcion     TEXT NOT NULL DEFAULT (datetime('now')),
    -- Las unidades quedan APARTADAS para el paciente durante la ventana (30 días por defecto); si no las
    -- reclama, el job de caducidad las devuelve a disponibles (trigger R4).
    horas_ventana_reclamo  INTEGER NOT NULL DEFAULT 720 CHECK (horas_ventana_reclamo BETWEEN 24 AND 720),
    -- Sin existencias al formular: queda PENDIENTE_STOCK y se aparta cuando llega el pedido (fecha_apartado);
    -- los 30 días para reclamar cuentan desde que se aparta, no desde que se formuló.
    fecha_apartado         TEXT,
    fecha_limite_reclamo   TEXT GENERATED ALWAYS AS
                           (datetime(COALESCE(fecha_apartado, fecha_prescripcion),
                                     '+' || horas_ventana_reclamo || ' hours')) STORED,
    estado                 TEXT NOT NULL DEFAULT 'VIGENTE'
                           CHECK (estado IN ('PENDIENTE_STOCK','VIGENTE','PARCIAL','ENTREGADA','CADUCADA','ANULADA')),
    requiere_reevaluacion  INTEGER NOT NULL DEFAULT 0 CHECK (requiere_reevaluacion IN (0,1)),
    CHECK (dosis_entregadas BETWEEN 0 AND dosis_prescritas)
);
CREATE INDEX ix_presc_caducidad ON prescripciones(estado, ambito, fecha_limite_reclamo);
CREATE INDEX ix_presc_paciente ON prescripciones(id_paciente, codigo_producto);

CREATE TABLE dispensaciones (
    id                INTEGER PRIMARY KEY,
    prescripcion_id   INTEGER NOT NULL REFERENCES prescripciones(id),
    usuario_id        INTEGER NOT NULL REFERENCES usuarios(id),         -- quien entrega
    cantidad          INTEGER NOT NULL CHECK (cantidad > 0),
    fecha             TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE administraciones_dosis (                                  -- registro de enfermería
    id                INTEGER PRIMARY KEY,
    prescripcion_id   INTEGER NOT NULL REFERENCES prescripciones(id),
    enfermero_id      INTEGER NOT NULL REFERENCES usuarios(id),
    fecha             TEXT NOT NULL DEFAULT (datetime('now')),
    resultado         TEXT NOT NULL CHECK (resultado IN ('ADMINISTRADA','OMITIDA','RECHAZADA_PACIENTE')),
    observacion       TEXT
);

-- Libro mayor de inventario: el stock NUNCA se edita, se deriva de la suma de movimientos
CREATE TABLE inventario_movimientos (
    id                INTEGER PRIMARY KEY,
    codigo_producto   TEXT NOT NULL REFERENCES productos_farmacia(codigo),
    tipo              TEXT NOT NULL CHECK (tipo IN ('SALDO_INICIAL','ENTRADA_COMPRA','RESERVA',
                        'DISPENSACION','LIBERACION_RESERVA','AJUSTE','BAJA_VENCIMIENTO')),
    delta_disponible  INTEGER NOT NULL,                  -- efecto sobre unidades disponibles
    delta_reservado   INTEGER NOT NULL,                  -- efecto sobre unidades reservadas
    prescripcion_id   INTEGER REFERENCES prescripciones(id),
    usuario_id        INTEGER REFERENCES usuarios(id),   -- NULL = proceso automático
    nota              TEXT,
    fecha             TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX ix_mov_producto ON inventario_movimientos(codigo_producto);
-- Idempotencia: una prescripción solo puede reservarse y liberarse UNA vez
CREATE UNIQUE INDEX ux_mov_reserva ON inventario_movimientos(prescripcion_id)
    WHERE tipo = 'RESERVA';
CREATE UNIQUE INDEX ux_mov_liberacion ON inventario_movimientos(prescripcion_id)
    WHERE tipo = 'LIBERACION_RESERVA';
CREATE TRIGGER trg_mov_no_update BEFORE UPDATE ON inventario_movimientos
BEGIN SELECT RAISE(ABORT, 'Los movimientos de inventario no se editan; registre un AJUSTE'); END;

CREATE VIEW v_stock AS
SELECT p.codigo, p.nombre, p.tipo_item, p.consumo_diario_promedio,
       COALESCE(SUM(m.delta_disponible), 0) AS disponible,
       COALESCE(SUM(m.delta_reservado), 0)  AS reservado
FROM productos_farmacia p LEFT JOIN inventario_movimientos m ON m.codigo_producto = p.codigo
GROUP BY p.codigo;

-- Semáforo de cobertura (visible solo con el permiso farmacia.alertas.ver)
CREATE VIEW v_semaforo_stock AS
SELECT codigo, nombre, tipo_item, disponible, reservado, consumo_diario_promedio,
       CASE WHEN consumo_diario_promedio > 0
            THEN ROUND(disponible / consumo_diario_promedio, 1) END AS dias_cobertura,
       CASE WHEN consumo_diario_promedio = 0 THEN 'SIN_CONSUMO'
            WHEN disponible / consumo_diario_promedio < 5  THEN 'ROJO'
            WHEN disponible / consumo_diario_promedio < 10 THEN 'AMARILLO'
            ELSE 'VERDE' END AS semaforo,
       MAX(CAST(ROUND(consumo_diario_promedio * 15 - disponible) AS INTEGER), 0) AS orden_sugerida_15d
FROM v_stock;

-- ---- Reglas del ciclo de vida (triggers) ------------------------------------

-- R1. Bloqueo por reevaluación: no se formula de nuevo un producto cuya fórmula caducó
--     hasta que exista una cita de REEVALUACION_FORMULA cumplida. Excepción: productos de
--     continuidad crítica (se permite, pero queda alerta en la HC).
CREATE TRIGGER trg_presc_bloqueo_reevaluacion BEFORE INSERT ON prescripciones
WHEN EXISTS (SELECT 1 FROM prescripciones p
             WHERE p.id_paciente = NEW.id_paciente AND p.codigo_producto = NEW.codigo_producto
               AND p.requiere_reevaluacion = 1)
 AND (SELECT critico_continuidad FROM productos_farmacia WHERE codigo = NEW.codigo_producto) = 0
BEGIN
    SELECT RAISE(ABORT, 'Entrega bloqueada: fórmula anterior caducada; requiere cita de reevaluación cumplida');
END;

-- R2. Al formular (ambulatoria): reservar unidades si hay stock disponible
CREATE TRIGGER trg_presc_reserva AFTER INSERT ON prescripciones
WHEN NEW.ambito = 'AMBULATORIA' AND NEW.estado <> 'PENDIENTE_STOCK'
BEGIN
    SELECT CASE WHEN (SELECT disponible FROM v_stock WHERE codigo = NEW.codigo_producto) < NEW.dosis_prescritas
                THEN RAISE(ABORT, 'Stock disponible insuficiente para reservar la fórmula') END;
    INSERT INTO inventario_movimientos(codigo_producto, tipo, delta_disponible, delta_reservado,
                                       prescripcion_id, usuario_id, nota, fecha)
    VALUES (NEW.codigo_producto, 'RESERVA', -NEW.dosis_prescritas, NEW.dosis_prescritas, NEW.id,
            NEW.medico_id, 'Apartado para el paciente por prescripción', NEW.fecha_prescripcion);
    INSERT INTO historia_clinica_eventos(historia_id, oid_ingreso, tipo, descripcion, autor_id, fecha)
    VALUES (NEW.historia_id, NEW.oid_ingreso, 'PRESCRIPCION',
            'Formula ' || NEW.dosis_prescritas || ' dosis de ' ||
            (SELECT nombre FROM productos_farmacia WHERE codigo = NEW.codigo_producto) ||
            ' (' || NEW.dosis || ' cada ' || NEW.frecuencia_horas || ' h); reclamar antes de ' ||
            NEW.fecha_limite_reclamo, NEW.medico_id, NEW.fecha_prescripcion);
END;

-- R2b. Formulado sin existencias: queda en espera y se deja constancia en la HC
CREATE TRIGGER trg_presc_pendiente AFTER INSERT ON prescripciones
WHEN NEW.estado = 'PENDIENTE_STOCK'
BEGIN
    INSERT INTO historia_clinica_eventos(historia_id, oid_ingreso, tipo, descripcion, autor_id, fecha)
    VALUES (NEW.historia_id, NEW.oid_ingreso, 'PRESCRIPCION',
            'Formula ' || NEW.dosis_prescritas || ' dosis de ' ||
            (SELECT nombre FROM productos_farmacia WHERE codigo = NEW.codigo_producto) ||
            ' SIN EXISTENCIAS: queda en espera y se aparta para el paciente cuando llegue el pedido',
            NEW.medico_id, NEW.fecha_prescripcion);
END;

-- R2c. Llegó el pedido: la fórmula en espera pasa a VIGENTE, se apartan las unidades y corre el plazo de 30 días
CREATE TRIGGER trg_presc_llego_stock AFTER UPDATE OF estado ON prescripciones
WHEN OLD.estado = 'PENDIENTE_STOCK' AND NEW.estado = 'VIGENTE'
BEGIN
    SELECT CASE WHEN (SELECT disponible FROM v_stock WHERE codigo = NEW.codigo_producto) < NEW.dosis_prescritas
                THEN RAISE(ABORT, 'Stock disponible insuficiente para apartar la fórmula en espera') END;
    INSERT INTO inventario_movimientos(codigo_producto, tipo, delta_disponible, delta_reservado,
                                       prescripcion_id, usuario_id, nota, fecha)
    VALUES (NEW.codigo_producto, 'RESERVA', -NEW.dosis_prescritas, NEW.dosis_prescritas, NEW.id, NULL,
            'Apartado al llegar el pedido (fórmula en espera)', NEW.fecha_apartado);
    INSERT INTO historia_clinica_eventos(historia_id, oid_ingreso, tipo, descripcion, autor_id, fecha)
    VALUES (NEW.historia_id, NEW.oid_ingreso, 'PRESCRIPCION',
            'Llegó el medicamento: ' || NEW.dosis_prescritas || ' dosis de ' ||
            (SELECT nombre FROM productos_farmacia WHERE codigo = NEW.codigo_producto) ||
            ' apartadas para el paciente hasta ' || NEW.fecha_limite_reclamo, NULL, NEW.fecha_apartado);
END;

-- Pedidos a proveedor: dan la fecha estimada de llegada que ve el paciente cuando no hay existencias
CREATE TABLE pedidos_compra (
    id                      INTEGER PRIMARY KEY,
    codigo_producto         TEXT NOT NULL REFERENCES productos_farmacia(codigo),
    cantidad                INTEGER NOT NULL CHECK (cantidad > 0),
    proveedor               TEXT,
    fecha_pedido            TEXT NOT NULL,
    fecha_estimada_llegada  TEXT NOT NULL,
    estado                  TEXT NOT NULL DEFAULT 'EN_CAMINO' CHECK (estado IN ('EN_CAMINO','RECIBIDO','CANCELADO')),
    fecha_recibido          TEXT,
    creado_por              INTEGER REFERENCES usuarios(id),
    CHECK (fecha_estimada_llegada >= substr(fecha_pedido, 1, 10))
);
CREATE INDEX ix_pedidos_producto ON pedidos_compra(codigo_producto, estado, fecha_estimada_llegada);

-- R3. Dispensar: solo fórmulas vigentes, dentro de la ventana y sin exceder lo prescrito
CREATE TRIGGER trg_disp_validar BEFORE INSERT ON dispensaciones
BEGIN
    SELECT CASE
      WHEN (SELECT estado FROM prescripciones WHERE id = NEW.prescripcion_id) = 'PENDIENTE_STOCK'
        THEN RAISE(ABORT, 'Medicamento sin existencias: se aparta para el paciente cuando llegue el pedido')
      WHEN (SELECT estado FROM prescripciones WHERE id = NEW.prescripcion_id) NOT IN ('VIGENTE','PARCIAL')
        THEN RAISE(ABORT, 'La fórmula no está vigente (caducada, entregada o anulada)')
      WHEN (SELECT ambito FROM prescripciones WHERE id = NEW.prescripcion_id) = 'AMBULATORIA'
       AND NEW.fecha > (SELECT fecha_limite_reclamo FROM prescripciones WHERE id = NEW.prescripcion_id)
        THEN RAISE(ABORT, 'Fuera de la ventana de reclamo')
      WHEN NEW.cantidad > (SELECT dosis_prescritas - dosis_entregadas FROM prescripciones
                           WHERE id = NEW.prescripcion_id)
        THEN RAISE(ABORT, 'La cantidad supera las dosis pendientes')
      WHEN (SELECT ambito FROM prescripciones WHERE id = NEW.prescripcion_id) = 'HOSPITALARIA'
       AND NEW.cantidad > (SELECT s.disponible FROM v_stock s JOIN prescripciones p
                           ON p.codigo_producto = s.codigo WHERE p.id = NEW.prescripcion_id)
        THEN RAISE(ABORT, 'Stock disponible insuficiente para la dispensación hospitalaria')
    END;
END;

CREATE TRIGGER trg_disp_aplicar AFTER INSERT ON dispensaciones
BEGIN
    UPDATE prescripciones
       SET dosis_entregadas = dosis_entregadas + NEW.cantidad,
           estado = CASE WHEN dosis_entregadas + NEW.cantidad = dosis_prescritas THEN 'ENTREGADA'
                         ELSE 'PARCIAL' END
     WHERE id = NEW.prescripcion_id;
    -- Ambulatoria: sale de lo reservado. Hospitalaria: sale directo de lo disponible.
    INSERT INTO inventario_movimientos(codigo_producto, tipo, delta_disponible, delta_reservado,
                                       prescripcion_id, usuario_id, nota, fecha)
    SELECT p.codigo_producto, 'DISPENSACION',
           CASE WHEN p.ambito = 'AMBULATORIA' THEN 0 ELSE -NEW.cantidad END,
           CASE WHEN p.ambito = 'AMBULATORIA' THEN -NEW.cantidad ELSE 0 END,
           p.id, NEW.usuario_id, 'Entrega al paciente', NEW.fecha
      FROM prescripciones p WHERE p.id = NEW.prescripcion_id;
    INSERT INTO historia_clinica_eventos(historia_id, oid_ingreso, tipo, descripcion, autor_id, fecha)
    SELECT p.historia_id, p.oid_ingreso, 'DISPENSACION',
           'Entregadas ' || NEW.cantidad || ' dosis de ' || f.nombre || ' (' || p.dosis_entregadas || '/' ||
           p.dosis_prescritas || ')', NEW.usuario_id, NEW.fecha
      FROM prescripciones p JOIN productos_farmacia f ON f.codigo = p.codigo_producto
     WHERE p.id = NEW.prescripcion_id;
END;

-- R4. Caducidad → retorno a stock + evento en HC + marca de reevaluación.
--     SQLite no tiene reloj interno: un job (pharmacy_service.expire_prescriptions) ejecuta el
--     UPDATE de estado y este trigger hace el resto dentro de la MISMA transacción.
CREATE TRIGGER trg_presc_caducada AFTER UPDATE OF estado ON prescripciones
WHEN NEW.estado = 'CADUCADA' AND OLD.estado IN ('VIGENTE','PARCIAL')
BEGIN
    INSERT INTO inventario_movimientos(codigo_producto, tipo, delta_disponible, delta_reservado,
                                       prescripcion_id, usuario_id, nota, fecha)
    VALUES (NEW.codigo_producto, 'LIBERACION_RESERVA',
            NEW.dosis_prescritas - NEW.dosis_entregadas, -(NEW.dosis_prescritas - NEW.dosis_entregadas),
            NEW.id, NULL, 'Retorno automático a stock por fórmula no reclamada', NEW.fecha_limite_reclamo);
    INSERT INTO historia_clinica_eventos(historia_id, oid_ingreso, tipo, descripcion, autor_id, fecha)
    VALUES (NEW.historia_id, NEW.oid_ingreso, 'FORMULA_CADUCADA',
            'Fórmula caducada - Medicamentos no reclamados en el periodo permitido (' ||
            (NEW.dosis_prescritas - NEW.dosis_entregadas) || ' dosis de ' ||
            (SELECT nombre FROM productos_farmacia WHERE codigo = NEW.codigo_producto) ||
            ' devueltas a inventario)', NULL, NEW.fecha_limite_reclamo);
    UPDATE prescripciones SET requiere_reevaluacion = 1 WHERE id = NEW.id;
END;

-- R4b. Anulación por el médico: libera la reserva pendiente, sin exigir reevaluación
CREATE TRIGGER trg_presc_anulada AFTER UPDATE OF estado ON prescripciones
WHEN NEW.estado = 'ANULADA' AND OLD.estado IN ('VIGENTE','PARCIAL') AND NEW.ambito = 'AMBULATORIA'
BEGIN
    INSERT INTO inventario_movimientos(codigo_producto, tipo, delta_disponible, delta_reservado,
                                       prescripcion_id, usuario_id, nota)
    VALUES (NEW.codigo_producto, 'LIBERACION_RESERVA',
            NEW.dosis_prescritas - NEW.dosis_entregadas, -(NEW.dosis_prescritas - NEW.dosis_entregadas),
            NEW.id, NEW.medico_id, 'Liberación por anulación de la fórmula');
    INSERT INTO historia_clinica_eventos(historia_id, oid_ingreso, tipo, descripcion, autor_id)
    VALUES (NEW.historia_id, NEW.oid_ingreso, 'PRESCRIPCION',
            'Fórmula anulada; ' || (NEW.dosis_prescritas - NEW.dosis_entregadas) || ' dosis liberadas', NEW.medico_id);
END;

-- R5. Cita de reevaluación cumplida → se levanta el bloqueo de ese producto para el paciente
CREATE TRIGGER trg_cita_reevaluacion_cumplida AFTER UPDATE OF estado ON citas
WHEN NEW.estado = 'CUMPLIDA' AND OLD.estado <> 'CUMPLIDA' AND NEW.motivo = 'REEVALUACION_FORMULA'
BEGIN
    UPDATE prescripciones SET requiere_reevaluacion = 0
     WHERE id_paciente = NEW.id_paciente
       AND codigo_producto = (SELECT codigo_producto FROM prescripciones WHERE id = NEW.prescripcion_origen_id);
END;

-- -----------------------------------------------------------------------------
-- 3b. Registro de pacientes, registros de historia clínica y adjuntos (CRUD con trazabilidad)
--     Res. 1995 de 1999: la historia clínica no se destruye. "Modificar" guarda la versión anterior
--     con quién, cuándo y por qué; "eliminar" es ANULAR con motivo (el registro sigue existiendo).
-- -----------------------------------------------------------------------------
CREATE TABLE pacientes_clinicos (
    id_paciente       INTEGER PRIMARY KEY,               -- HIS: mismo id del extracto; nuevos: desde 1.000.000
    origen            TEXT NOT NULL CHECK (origen IN ('HIS','REGISTRO')),
    tipo_documento    TEXT,
    numero_documento  TEXT,
    nombres           TEXT NOT NULL,
    apellidos         TEXT NOT NULL DEFAULT '',
    fecha_nacimiento  TEXT,
    edad              INTEGER CHECK (edad IS NULL OR edad BETWEEN 0 AND 120),
    sexo              TEXT CHECK (sexo IS NULL OR sexo IN ('Femenino','Masculino','Intersexual')),
    telefono          TEXT,
    direccion         TEXT,
    asegurador        TEXT,
    regimen           TEXT,
    municipio         TEXT,
    correo            TEXT,                              -- lo registra admisiones: habilita la cuenta del portal
    estado            TEXT NOT NULL DEFAULT 'ACTIVO' CHECK (estado IN ('ACTIVO','INACTIVO')),
    creado_por        INTEGER REFERENCES usuarios(id),
    creado_en         TEXT NOT NULL DEFAULT (datetime('now')),
    actualizado_por   INTEGER REFERENCES usuarios(id),
    actualizado_en    TEXT,
    CHECK (origen = 'HIS' OR numero_documento IS NOT NULL)
);
CREATE UNIQUE INDEX ux_paciente_documento ON pacientes_clinicos(tipo_documento, numero_documento)
    WHERE numero_documento IS NOT NULL;
CREATE TRIGGER trg_paciente_no_delete BEFORE DELETE ON pacientes_clinicos
BEGIN SELECT RAISE(ABORT, 'Los pacientes no se eliminan; márquelo como INACTIVO'); END;

CREATE TABLE hc_registros (
    id                 INTEGER PRIMARY KEY,
    historia_id        INTEGER NOT NULL REFERENCES historias_clinicas(id),
    tipo               TEXT NOT NULL CHECK (tipo IN ('CONSULTA','EVOLUCION','ANTECEDENTES','RESULTADO_EXAMEN',
                                                     'EPICRISIS')),
    titulo             TEXT NOT NULL CHECK (length(trim(titulo)) >= 3),
    contenido          TEXT NOT NULL CHECK (length(trim(contenido)) >= 10),
    diagnostico_cie10  TEXT,
    diagnostico_nombre TEXT,
    plan               TEXT,
    autor_id           INTEGER NOT NULL REFERENCES usuarios(id),
    creado_en          TEXT NOT NULL,
    version            INTEGER NOT NULL DEFAULT 1,
    actualizado_por    INTEGER REFERENCES usuarios(id),
    actualizado_en     TEXT,
    motivo_cambio      TEXT,
    estado             TEXT NOT NULL DEFAULT 'ACTIVO' CHECK (estado IN ('ACTIVO','ANULADO')),
    motivo_anulacion   TEXT,
    origen_externo     TEXT,                              -- importado: sistema, autor y fecha originales
    CHECK (estado = 'ACTIVO' OR motivo_anulacion IS NOT NULL),
    CHECK (version = 1 OR motivo_cambio IS NOT NULL)
);
CREATE INDEX ix_hc_registros ON hc_registros(historia_id, creado_en);

-- Versiones anteriores (append-only)
CREATE TABLE hc_registros_versiones (
    id                 INTEGER PRIMARY KEY,
    registro_id        INTEGER NOT NULL REFERENCES hc_registros(id),
    version            INTEGER NOT NULL,
    titulo             TEXT NOT NULL,
    contenido          TEXT NOT NULL,
    diagnostico_cie10  TEXT,
    diagnostico_nombre TEXT,
    plan               TEXT,
    vigente_desde      TEXT NOT NULL,
    reemplazada_en     TEXT NOT NULL,
    reemplazada_por    INTEGER REFERENCES usuarios(id),
    motivo_reemplazo   TEXT NOT NULL,
    UNIQUE (registro_id, version)
);
CREATE TRIGGER trg_hc_versiones_no_update BEFORE UPDATE ON hc_registros_versiones
BEGIN SELECT RAISE(ABORT, 'Las versiones de la historia clínica no se modifican'); END;
CREATE TRIGGER trg_hc_versiones_no_delete BEFORE DELETE ON hc_registros_versiones
BEGIN SELECT RAISE(ABORT, 'Las versiones de la historia clínica no se eliminan'); END;

CREATE TRIGGER trg_hc_registro_no_delete BEFORE DELETE ON hc_registros
BEGIN SELECT RAISE(ABORT, 'Los registros de la historia clínica no se eliminan; anúlelos con un motivo'); END;

CREATE TRIGGER trg_hc_registro_anulado_inmutable BEFORE UPDATE ON hc_registros
WHEN OLD.estado = 'ANULADO'
BEGIN SELECT RAISE(ABORT, 'Un registro anulado no se puede modificar'); END;

-- Corrección: versión +1, motivo obligatorio y copia de lo anterior
CREATE TRIGGER trg_hc_registro_correccion BEFORE UPDATE OF titulo, contenido, diagnostico_cie10,
    diagnostico_nombre, plan ON hc_registros
WHEN NEW.estado = 'ACTIVO'
BEGIN
    SELECT CASE
      WHEN NEW.version <> OLD.version + 1 THEN RAISE(ABORT, 'Cada corrección debe subir la versión en 1')
      WHEN NEW.motivo_cambio IS NULL OR length(trim(NEW.motivo_cambio)) < 10
        THEN RAISE(ABORT, 'Escribe el motivo de la corrección (mínimo 10 caracteres)')
    END;
    INSERT INTO hc_registros_versiones(registro_id, version, titulo, contenido, diagnostico_cie10,
        diagnostico_nombre, plan, vigente_desde, reemplazada_en, reemplazada_por, motivo_reemplazo)
    VALUES (OLD.id, OLD.version, OLD.titulo, OLD.contenido, OLD.diagnostico_cie10, OLD.diagnostico_nombre,
            OLD.plan, COALESCE(OLD.actualizado_en, OLD.creado_en), NEW.actualizado_en, NEW.actualizado_por,
            NEW.motivo_cambio);
END;

CREATE TRIGGER trg_hc_registro_evento_nuevo AFTER INSERT ON hc_registros
BEGIN
    INSERT INTO historia_clinica_eventos(historia_id, tipo, descripcion, autor_id, fecha)
    VALUES (NEW.historia_id, 'REGISTRO_HC', 'Nuevo registro (' || lower(replace(NEW.tipo, '_', ' ')) || '): ' ||
            NEW.titulo, NEW.autor_id, NEW.creado_en);
END;

CREATE TRIGGER trg_hc_registro_evento_cambio AFTER UPDATE ON hc_registros
WHEN NEW.version > OLD.version OR (NEW.estado = 'ANULADO' AND OLD.estado = 'ACTIVO')
BEGIN
    INSERT INTO historia_clinica_eventos(historia_id, tipo, descripcion, autor_id, fecha)
    VALUES (NEW.historia_id, 'REGISTRO_HC',
            CASE WHEN NEW.estado = 'ANULADO'
                 THEN 'Registro anulado: ' || NEW.titulo || ' · motivo: ' || NEW.motivo_anulacion
                 ELSE 'Registro corregido (versión ' || NEW.version || '): ' || NEW.titulo || ' · motivo: ' ||
                      NEW.motivo_cambio END,
            NEW.actualizado_por, NEW.actualizado_en);
END;

-- Adjuntos (PDF o imagen de exámenes, epicrisis externas…). Máximo 5 MB.
CREATE TABLE hc_adjuntos (
    id                INTEGER PRIMARY KEY,
    historia_id       INTEGER NOT NULL REFERENCES historias_clinicas(id),
    registro_id       INTEGER REFERENCES hc_registros(id),
    nombre_archivo    TEXT NOT NULL,
    tipo_mime         TEXT NOT NULL CHECK (tipo_mime IN ('application/pdf','image/png','image/jpeg')),
    tamano_bytes      INTEGER NOT NULL CHECK (tamano_bytes BETWEEN 1 AND 5242880),
    sha256            TEXT NOT NULL,
    contenido         BLOB NOT NULL,
    descripcion       TEXT,
    subido_por        INTEGER NOT NULL REFERENCES usuarios(id),
    subido_en         TEXT NOT NULL,
    estado            TEXT NOT NULL DEFAULT 'ACTIVO' CHECK (estado IN ('ACTIVO','ANULADO')),
    anulado_por       INTEGER REFERENCES usuarios(id),
    anulado_en        TEXT,
    motivo_anulacion  TEXT,
    CHECK (estado = 'ACTIVO' OR motivo_anulacion IS NOT NULL)
);
CREATE INDEX ix_hc_adjuntos ON hc_adjuntos(historia_id);
CREATE TRIGGER trg_adjunto_no_delete BEFORE DELETE ON hc_adjuntos
BEGIN SELECT RAISE(ABORT, 'Los adjuntos no se eliminan; anúlelos con un motivo'); END;
CREATE TRIGGER trg_adjunto_contenido_inmutable BEFORE UPDATE OF contenido, sha256, nombre_archivo ON hc_adjuntos
BEGIN SELECT RAISE(ABORT, 'Un adjunto no se reemplaza: anúlelo y suba uno nuevo'); END;
CREATE TRIGGER trg_adjunto_evento AFTER INSERT ON hc_adjuntos
BEGIN
    INSERT INTO historia_clinica_eventos(historia_id, tipo, descripcion, autor_id, fecha)
    VALUES (NEW.historia_id, 'ADJUNTO', 'Adjunto cargado: ' || NEW.nombre_archivo ||
            COALESCE(' · ' || NEW.descripcion, ''), NEW.subido_por, NEW.subido_en);
END;
CREATE TRIGGER trg_adjunto_evento_anulado AFTER UPDATE OF estado ON hc_adjuntos
WHEN NEW.estado = 'ANULADO' AND OLD.estado = 'ACTIVO'
BEGIN
    INSERT INTO historia_clinica_eventos(historia_id, tipo, descripcion, autor_id, fecha)
    VALUES (NEW.historia_id, 'ADJUNTO', 'Adjunto anulado: ' || NEW.nombre_archivo || ' · motivo: ' ||
            NEW.motivo_anulacion, NEW.anulado_por, NEW.anulado_en);
END;

-- Ficha clínica del paciente: alergias, antecedentes, medicación habitual (una por paciente, con versiones)
CREATE TABLE hc_ficha (
    id_paciente               INTEGER PRIMARY KEY,
    grupo_sanguineo           TEXT CHECK (grupo_sanguineo IS NULL OR grupo_sanguineo IN
                                  ('O+','O-','A+','A-','B+','B-','AB+','AB-')),
    alergias                  TEXT,                     -- "Penicilina (urticaria); Sulfas"; NULL = sin dato
    sin_alergias_conocidas    INTEGER NOT NULL DEFAULT 0 CHECK (sin_alergias_conocidas IN (0,1)),
    antecedentes_personales   TEXT,
    antecedentes_quirurgicos  TEXT,
    antecedentes_familiares   TEXT,
    medicacion_habitual       TEXT,
    habitos                   TEXT,
    contacto_emergencia       TEXT,
    telefono_emergencia       TEXT,
    observaciones             TEXT,
    actualizado_por           INTEGER REFERENCES usuarios(id),
    actualizado_en            TEXT NOT NULL,
    CHECK (NOT (sin_alergias_conocidas = 1 AND alergias IS NOT NULL))
);
CREATE TABLE hc_ficha_versiones (
    id            INTEGER PRIMARY KEY,
    id_paciente   INTEGER NOT NULL,
    contenido     TEXT NOT NULL,                        -- JSON de la ficha anterior
    vigente_desde TEXT NOT NULL,
    reemplazada_en TEXT NOT NULL,
    reemplazada_por INTEGER REFERENCES usuarios(id)
);
CREATE TRIGGER trg_ficha_version BEFORE UPDATE ON hc_ficha
BEGIN
    INSERT INTO hc_ficha_versiones(id_paciente, contenido, vigente_desde, reemplazada_en, reemplazada_por)
    VALUES (OLD.id_paciente, json_object('grupo_sanguineo', OLD.grupo_sanguineo, 'alergias', OLD.alergias,
            'sin_alergias_conocidas', OLD.sin_alergias_conocidas, 'antecedentes_personales', OLD.antecedentes_personales,
            'antecedentes_quirurgicos', OLD.antecedentes_quirurgicos, 'antecedentes_familiares',
            OLD.antecedentes_familiares, 'medicacion_habitual', OLD.medicacion_habitual, 'habitos', OLD.habitos,
            'contacto_emergencia', OLD.contacto_emergencia, 'telefono_emergencia', OLD.telefono_emergencia,
            'observaciones', OLD.observaciones), OLD.actualizado_en, NEW.actualizado_en, NEW.actualizado_por);
END;
CREATE TRIGGER trg_ficha_no_delete BEFORE DELETE ON hc_ficha
BEGIN SELECT RAISE(ABORT, 'La ficha clínica no se elimina'); END;

-- Asignación manual de camas (sobre el censo del extracto): ocupar con estancia estimada o liberar
CREATE TABLE camas_asignaciones (
    id               INTEGER PRIMARY KEY,
    codigo_cama      TEXT NOT NULL,
    accion           TEXT NOT NULL CHECK (accion IN ('OCUPAR','LIBERAR')),
    id_paciente      INTEGER,
    dias_estimados   INTEGER CHECK (dias_estimados IS NULL OR dias_estimados BETWEEN 1 AND 90),
    motivo           TEXT,
    usuario_id       INTEGER NOT NULL REFERENCES usuarios(id),
    fecha            TEXT NOT NULL,
    CHECK (accion = 'LIBERAR' OR (id_paciente IS NOT NULL AND dias_estimados IS NOT NULL)),
    CHECK (accion = 'OCUPAR' OR motivo IS NOT NULL)
);
CREATE INDEX ix_camas_asig ON camas_asignaciones(codigo_cama, fecha);
CREATE TRIGGER trg_camas_asig_no_update BEFORE UPDATE ON camas_asignaciones
BEGIN SELECT RAISE(ABORT, 'Los movimientos de cama no se editan; registre uno nuevo'); END;
CREATE TRIGGER trg_camas_asig_no_delete BEFORE DELETE ON camas_asignaciones
BEGIN SELECT RAISE(ABORT, 'Los movimientos de cama no se eliminan'); END;
CREATE TRIGGER trg_camas_asig_evento AFTER INSERT ON camas_asignaciones
WHEN NEW.id_paciente IS NOT NULL
 AND EXISTS (SELECT 1 FROM historias_clinicas WHERE id_paciente = NEW.id_paciente)
BEGIN
    INSERT INTO historia_clinica_eventos(historia_id, tipo, descripcion, autor_id, fecha)
    SELECT id, 'DATOS_PACIENTE',
           CASE NEW.accion WHEN 'OCUPAR' THEN 'Asignada la cama ' || NEW.codigo_cama || ' (estancia estimada ' ||
                NEW.dias_estimados || ' días)'
                ELSE 'Liberada la cama ' || NEW.codigo_cama || ' · ' || NEW.motivo END,
           NEW.usuario_id, NEW.fecha
      FROM historias_clinicas WHERE id_paciente = NEW.id_paciente;
END;

-- -----------------------------------------------------------------------------
-- 3c. Turnos de atención (fila de espera con código), recuperación de contraseña
-- -----------------------------------------------------------------------------
CREATE TABLE turnos_atencion (
    id            INTEGER PRIMARY KEY,
    fecha         TEXT NOT NULL,                        -- día (AAAA-MM-DD): los códigos se reinician cada día
    servicio      TEXT NOT NULL CHECK (servicio IN ('CONSULTA','FARMACIA','LABORATORIO','ADMISIONES')),
    numero        INTEGER NOT NULL,
    codigo        TEXT NOT NULL,                        -- C-007, F-012, L-003, A-021
    id_paciente   INTEGER NOT NULL,
    cita_id       INTEGER REFERENCES citas(id),
    prioridad     INTEGER NOT NULL DEFAULT 0 CHECK (prioridad IN (0,1)),   -- 1 = adulto mayor, gestante, discapacidad
    estado        TEXT NOT NULL DEFAULT 'EN_ESPERA'
                  CHECK (estado IN ('EN_ESPERA','LLAMADO','EN_ATENCION','ATENDIDO','NO_SE_PRESENTO')),
    modulo        TEXT,                                 -- consultorio o ventanilla que llama
    creado_por    INTEGER REFERENCES usuarios(id),
    creado_en     TEXT NOT NULL,
    llamado_en    TEXT,
    atendido_en   TEXT,
    atendido_por  INTEGER REFERENCES usuarios(id),
    UNIQUE (fecha, servicio, numero)
);
CREATE INDEX ix_turnos_atencion ON turnos_atencion(fecha, servicio, estado);
CREATE UNIQUE INDEX ux_turno_activo_paciente ON turnos_atencion(fecha, servicio, id_paciente)
    WHERE estado IN ('EN_ESPERA','LLAMADO','EN_ATENCION');

CREATE TABLE recuperacion_clave (
    id           INTEGER PRIMARY KEY,
    usuario_id   INTEGER NOT NULL REFERENCES usuarios(id),
    codigo_hash  TEXT NOT NULL,                         -- nunca el código en claro
    creado_en    TEXT NOT NULL,
    expira_en    TEXT NOT NULL,
    usado_en     TEXT,
    intentos     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_recuperacion ON recuperacion_clave(usuario_id, creado_en);

-- Auto-registro del paciente en el portal: documento + el correo que registró admisiones -> código de 6 dígitos
CREATE TABLE registro_pacientes (
    id           INTEGER PRIMARY KEY,
    id_paciente  INTEGER NOT NULL REFERENCES pacientes_clinicos(id_paciente),
    correo       TEXT NOT NULL,
    codigo_hash  TEXT NOT NULL,                         -- nunca el código en claro
    creado_en    TEXT NOT NULL,
    expira_en    TEXT NOT NULL,
    usado_en     TEXT,
    intentos     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_registro_pacientes ON registro_pacientes(id_paciente, creado_en);

-- Lista de espera quirúrgica y programación (capacidad en cirugías/día por área: ver surgery_planner.py)
CREATE TABLE cirugias_solicitudes (
    id                        INTEGER PRIMARY KEY,
    origen                    TEXT NOT NULL CHECK (origen IN ('HIS','APP')),
    consecutivo_programacion  TEXT UNIQUE,              -- HIS: programación sin evidencia de ejecución
    id_paciente               INTEGER NOT NULL,
    area_quirofano            TEXT NOT NULL,
    procedimientos            INTEGER NOT NULL DEFAULT 1,
    codigos_cups              TEXT,
    prioridad                 TEXT NOT NULL CHECK (prioridad IN ('URGENTE','PRIORITARIA','ELECTIVA')),
    fecha_solicitud           TEXT NOT NULL,
    estado                    TEXT NOT NULL DEFAULT 'EN_ESPERA'
                              CHECK (estado IN ('EN_ESPERA','PROGRAMADA','REALIZADA','CANCELADA')),
    fecha_programada          TEXT,
    nota                      TEXT,
    motivo                    TEXT,
    motivo_categoria          TEXT,
    sobrecupo_justificacion   TEXT,                      -- programada por encima de la capacidad probada
    creado_por                INTEGER REFERENCES usuarios(id),
    actualizado_por           INTEGER REFERENCES usuarios(id),
    actualizado_en            TEXT,
    CHECK (estado <> 'PROGRAMADA' OR fecha_programada IS NOT NULL),
    CHECK (estado <> 'CANCELADA' OR motivo IS NOT NULL)
);
CREATE INDEX ix_cirugias_sol ON cirugias_solicitudes(estado, area_quirofano, fecha_programada);
CREATE TRIGGER trg_cirugias_sol_no_delete BEFORE DELETE ON cirugias_solicitudes
BEGIN SELECT RAISE(ABORT, 'Las solicitudes quirúrgicas no se eliminan; cancélelas con un motivo'); END;

-- -----------------------------------------------------------------------------
-- 4. Datos semilla: roles y matriz de permisos
-- -----------------------------------------------------------------------------
INSERT INTO roles(id, codigo, nombre) VALUES
 (1,'ADMIN','Administrador'), (2,'DOCTOR','Médico'), (3,'ENFERMERIA','Enfermería'), (4,'PACIENTE','Paciente'),
 (5,'FACTURACION','Facturación y admisiones'), (6,'QUIROFANOS','Coordinación de quirófanos');

INSERT INTO permisos(codigo, descripcion) VALUES
 ('tablero.gerencial.ver',      'KPIs hospitalarios y métricas gerenciales'),
 ('agente.consultar',           'Asistente NL2SQL sobre la base analítica anonimizada'),
 ('usuarios.administrar',       'Crear usuarios, cambiar roles y estados'),
 ('auditoria.ver',              'Bitácora de accesos'),
 ('inventario.auditar',         'Movimientos y ajustes de inventario'),
 ('farmacia.alertas.ver',       'Semáforo de stock y alertas de desabastecimiento'),
 ('farmacia.orden_compra',      'Exportar orden de compra CSV'),
 ('camas.ver',                  'Ocupación de camas por servicio'),
 ('hc.ver_completa',            'Historia clínica completa de pacientes a cargo'),
 ('hc.ver_notas',               'Notas clínicas y órdenes vigentes'),
 ('hc.acceso_emergencia',       'Romper el vidrio: acceso excepcional auditado'),
 ('prescripcion.crear',         'Formular medicamentos'),
 ('interconsulta.solicitar',    'Solicitar interconsultas'),
 ('dispensacion.registrar',     'Entregar medicamentos de farmacia'),
 ('dosis.registrar',            'Registrar administración de dosis'),
 ('portal.propio',              'Citas, fórmulas e historial PROPIOS'),
 ('pacientes.registrar',        'Registrar pacientes y actualizar sus datos'),
 ('hc.registrar',               'Crear, corregir y anular registros de historia clínica y adjuntos'),
 ('hc.buscar',                  'Buscar historias clínicas'),
 ('hc.exportar',                'Descargar en PDF, exportar e importar historias clínicas'),
 ('camas.gestionar',            'Ocupar y liberar camas'),
 ('citas.gestionar',            'Agendar, reprogramar y cancelar citas de cualquier paciente'),
 ('citas.agenda',               'Ver la agenda propia y atender citas'),
 ('turnos_atencion.gestionar',  'Dar y llamar turnos de atención (fila de espera)'),
 ('personal.turnos',            'Asignar turnos de trabajo al personal'),
 ('quirofanos.ver',             'Ver indicadores, lista de espera y programación de quirófanos'),
 ('quirofanos.solicitar',       'Solicitar cirugías y cancelar las propias en espera'),
 ('quirofanos.coordinar',       'Programar, reprogramar, cancelar y cerrar cirugías (coordinación de quirófanos)');

INSERT INTO rol_permisos(rol_id, permiso_id)
SELECT r.id, p.id FROM roles r JOIN permisos p ON
  (r.codigo = 'ADMIN'      AND p.codigo IN ('tablero.gerencial.ver','agente.consultar','usuarios.administrar',
                                             'auditoria.ver','inventario.auditar','farmacia.alertas.ver',
                                             'farmacia.orden_compra','camas.ver','pacientes.registrar',
                                             'hc.buscar','hc.ver_completa','hc.exportar','camas.gestionar',
                                             'personal.turnos','quirofanos.ver'))
  OR (r.codigo = 'DOCTOR'  AND p.codigo IN ('agente.consultar','farmacia.alertas.ver','camas.ver','hc.ver_completa',
                                             'hc.ver_notas','hc.acceso_emergencia','prescripcion.crear',
                                             'interconsulta.solicitar','pacientes.registrar','hc.registrar',
                                             'hc.buscar','hc.exportar','camas.gestionar','citas.agenda',
                                             'turnos_atencion.gestionar','quirofanos.ver','quirofanos.solicitar'))
  OR (r.codigo = 'ENFERMERIA' AND p.codigo IN ('farmacia.alertas.ver','camas.ver','hc.ver_notas',
                                             'hc.acceso_emergencia','dispensacion.registrar','dosis.registrar',
                                             'hc.buscar','camas.gestionar'))
  OR (r.codigo = 'PACIENTE' AND p.codigo IN ('portal.propio'))
  OR (r.codigo = 'FACTURACION' AND p.codigo IN ('pacientes.registrar','citas.gestionar',
                                             'turnos_atencion.gestionar','camas.ver'))
  OR (r.codigo = 'QUIROFANOS' AND p.codigo IN ('quirofanos.ver','quirofanos.solicitar','quirofanos.coordinar',
                                             'camas.ver'));

-- Versión del esquema: si una clinico.db vieja tiene otra, se respalda y se recrea (pharmacy_service)
PRAGMA user_version = 9;
