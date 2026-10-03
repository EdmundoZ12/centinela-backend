# S4 — Descuentos fuera de política

## Contrato

El Vigía consulta `centinela.v_descuentos_fuera_politica`, agrupa por vendedor y
semana y crea una sola alerta `DISCOUNT_POLICY_VIOLATION`. La clave de deduplicación
es tipo/vendedor/semana. La severidad es `HIGH` para una semana y `CRITICAL` cuando
el backend demuestra al menos dos semanas consecutivas.

`amount_at_risk` es la suma de `descuento_en_exceso` de las líneas verificadas.
Representa descuento observado por encima del tope normal; no es pérdida, fraude,
ahorro ni recuperación garantizada.

Los topes normales permanecen en `centinela.ref_topes_descuento`. Los topes
especiales documentados se estructuran en `app.discount_policy_limits` mediante
`09_discount_scenario.sql`. Las líneas con aprobación `S` que excedan el tope
especial se conservan como observaciones de análisis. No sustituyen la regla
oficial de detección definida por `resources/metricas.yaml`.

## Aplicación

Después de `08_executor.sql`:

```powershell
.\.venv\Scripts\python.exe database\scripts\apply_discount_scenario.py
```

El script no reconstruye el dataset ni ejecuta el Vigía.

## Pruebas

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Para habilitar las pruebas PostgreSQL existentes:

```powershell
$env:CENTINELA_TEST_POSTGRES = "1"
try {
    .\.venv\Scripts\python.exe -m unittest discover -s tests -p "*postgres.py" -v
} finally {
    Remove-Item Env:\CENTINELA_TEST_POSTGRES
}
```

## Integración real opcional

Consume embeddings y Responses API, persiste análisis y estrategia, y se detiene
antes de aprobar o ejecutar:

```powershell
.\.venv\Scripts\python.exe scripts\integration_s4_full.py --alert-id UUID --debug
```

## Acciones sandbox

- `CREATE_DISCOUNT_REVIEW_TASK`
- `CREATE_SELLER_COACHING_TASK`
- `CREATE_QUOTING_PERMISSION_REVIEW`, solo con reincidencia calculada
- `CREATE_COMMERCIAL_MANAGER_REVIEW`

Todas crean tareas internas. No modifican pedidos, descuentos ni permisos reales.
