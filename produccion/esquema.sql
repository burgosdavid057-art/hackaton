-- Esquema de produccion.db  —  Copiloto de Producción HACEB
--
-- Tres decisiones que vale la pena defender:
--
-- 1. Cada hecho apunta a documento_id. Cualquier cifra del resumen ejecutivo se
--    puede rastrear hasta el archivo original que la produjo. Sin esto el agente
--    no es auditable y un supervisor no tiene por qué creerle.
-- 2. causa_id puede ser NULL y causa_texto nunca lo es. Lo que el modelo no supo
--    clasificar no se pierde ni se inventa: queda en la cola de revisión.
-- 3. Los costos son una tabla con vigencia, no constantes en el código. El precio
--    del kg de lámina cambia y el histórico no se debe reescribir.

PRAGMA foreign_keys = ON;

-- ── Catálogos (Fase 0: se cargan desde taxonomia.yaml) ──────────────────────

CREATE TABLE lineas (
    id      INTEGER PRIMARY KEY,
    nombre  TEXT NOT NULL UNIQUE,
    activa  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE estaciones (
    id       INTEGER PRIMARY KEY,
    linea_id INTEGER NOT NULL REFERENCES lineas(id),
    nombre   TEXT NOT NULL,
    alias    TEXT,                    -- JSON: ["remachadora","remach","R-02"]
    UNIQUE (linea_id, nombre)
);

CREATE TABLE causas (
    id        INTEGER PRIMARY KEY,
    tipo      TEXT NOT NULL CHECK (tipo IN ('parada', 'scrap', 'calidad')),
    codigo    TEXT NOT NULL UNIQUE,   -- PAR-MEC-01
    nombre    TEXT NOT NULL,
    categoria TEXT,                   -- mecánica | material | calidad | personal | setup
    alias     TEXT                    -- JSON, para el fuzzy match
);

-- ── Trazabilidad ────────────────────────────────────────────────────────────

CREATE TABLE documentos (
    id         INTEGER PRIMARY KEY,
    ruta       TEXT NOT NULL,
    sha256     TEXT NOT NULL UNIQUE,  -- idempotencia: el mismo archivo no entra dos veces
    formato    TEXT NOT NULL,         -- xlsx | pdf | csv | txt
    cargado_en TEXT NOT NULL,
    texto_crudo TEXT NOT NULL         -- lo que vio el extractor, para verificar literalidad
);

-- ── Hechos ──────────────────────────────────────────────────────────────────

CREATE TABLE turnos (
    id                  INTEGER PRIMARY KEY,
    documento_id        INTEGER NOT NULL REFERENCES documentos(id),
    fecha               TEXT NOT NULL,
    turno               INTEGER CHECK (turno IN (1, 2, 3)),
    linea_id            INTEGER REFERENCES lineas(id),
    supervisor          TEXT,
    unidades_plan       REAL,
    unidades_producidas REAL,
    minutos_turno       REAL,
    UNIQUE (fecha, turno, linea_id)
);

CREATE TABLE paradas (
    id           INTEGER PRIMARY KEY,
    turno_id     INTEGER NOT NULL REFERENCES turnos(id) ON DELETE CASCADE,
    estacion_id  INTEGER REFERENCES estaciones(id),
    causa_id     INTEGER REFERENCES causas(id),
    causa_texto  TEXT NOT NULL,
    minutos      REAL,
    hora_inicio  TEXT,
    confianza    REAL NOT NULL DEFAULT 1.0,
    revisado     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE scrap (
    id           INTEGER PRIMARY KEY,
    turno_id     INTEGER NOT NULL REFERENCES turnos(id) ON DELETE CASCADE,
    estacion_id  INTEGER REFERENCES estaciones(id),
    causa_id     INTEGER REFERENCES causas(id),
    causa_texto  TEXT NOT NULL,
    unidades     REAL,
    kg           REAL,
    confianza    REAL NOT NULL DEFAULT 1.0,
    revisado     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE calidad (
    id           INTEGER PRIMARY KEY,
    turno_id     INTEGER NOT NULL REFERENCES turnos(id) ON DELETE CASCADE,
    tipo_defecto TEXT NOT NULL,
    unidades     REAL,
    descripcion  TEXT,
    confianza    REAL NOT NULL DEFAULT 1.0,
    revisado     INTEGER NOT NULL DEFAULT 0
);

-- ── Costeo (Fase 3) ─────────────────────────────────────────────────────────

CREATE TABLE costos (
    clave          TEXT NOT NULL,     -- cop_por_minuto_parada_L2 | cop_por_kg_scrap_lamina
    valor          REAL NOT NULL,
    unidad         TEXT NOT NULL,
    vigente_desde  TEXT NOT NULL,
    fuente         TEXT NOT NULL,     -- de dónde salió; si es un supuesto, decirlo
    PRIMARY KEY (clave, vigente_desde)
);

-- ── Índices para las consultas del agente ───────────────────────────────────

CREATE INDEX idx_turnos_fecha    ON turnos (fecha);
CREATE INDEX idx_paradas_causa   ON paradas (causa_id);
CREATE INDEX idx_scrap_causa     ON scrap (causa_id);
CREATE INDEX idx_paradas_rev     ON paradas (revisado) WHERE revisado = 0;
CREATE INDEX idx_scrap_rev       ON scrap (revisado) WHERE revisado = 0;
