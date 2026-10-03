-- Aplicar sobre el dataset y la capa semántica oficiales ya instalados.
-- No reinicia un reloj existente ni modifica los CSV.
CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.simulation_state (
    id smallint PRIMARY KEY CHECK (id = 1),
    "current_date" date NOT NULL CHECK (
        "current_date" BETWEEN DATE '2026-06-30' AND DATE '2026-09-30'
    ),
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO app.simulation_state (id, "current_date")
VALUES (1, DATE '2026-06-30')
ON CONFLICT (id) DO NOTHING;

CREATE OR REPLACE FUNCTION centinela.fecha_corte()
RETURNS date LANGUAGE sql STABLE AS $$
    SELECT s."current_date" FROM app.simulation_state AS s WHERE s.id = 1
$$;

-- Ventas: excluir pedidos que todavía no ocurrieron.
-- v_margen_semanal_linea y v_descuentos_fuera_politica heredan este filtro.
CREATE OR REPLACE VIEW centinela.v_ventas AS
SELECT p.pedido_id, p.fecha, p.cliente_id, c.nombre AS cliente, c.segmento, p.vendedor_id, p.ciudad, c.region,
       d.linea_n, d.sku, pr.nombre AS producto, pr.linea, d.cantidad, d.precio_lista, d.precio_unitario,
       d.descuento_pct, d.aprobacion_especial, d.valor_neto, d.cantidad * d.costo_unitario AS costo_total,
       d.valor_neto - d.cantidad * d.costo_unitario AS margen_bruto, p.estado
FROM centinela.pedidos p JOIN centinela.pedidos_detalle d USING (pedido_id)
JOIN centinela.clientes c USING (cliente_id) JOIN centinela.productos pr USING (sku)
WHERE p.estado <> 'Cancelado' AND p.fecha <= centinela.fecha_corte();

-- Pagos: ni la factura ni el pago pueden ser posteriores al corte.
CREATE OR REPLACE VIEW centinela.v_dias_pago_mensual AS
SELECT f.cliente_id, date_trunc('month', f.fecha_factura)::date AS mes_factura,
       round(avg(p.fecha_pago - f.fecha_factura), 1) AS dias_pago_prom, count(*) AS facturas_pagadas
FROM centinela.facturas f JOIN centinela.pagos p USING (factura_id)
WHERE f.fecha_factura <= centinela.fecha_corte() AND p.fecha_pago <= centinela.fecha_corte()
GROUP BY 1, 2;

-- Inventario: demanda y existencias ya estaban acotadas; faltaba acotar pedidos pendientes.
CREATE OR REPLACE VIEW centinela.v_cobertura_inventario AS
WITH dem AS (SELECT sku, bodega_id, avg(salidas) AS demanda_prom_30d FROM centinela.inventario_diario
             WHERE fecha > centinela.fecha_corte() - 30 AND fecha <= centinela.fecha_corte() GROUP BY 1, 2),
pend AS (SELECT d.sku, b.bodega_id, sum(d.cantidad) AS unidades_pendientes
         FROM centinela.pedidos p JOIN centinela.pedidos_detalle d USING (pedido_id)
         JOIN (VALUES ('Medellín','BOD-MDE'),('Pereira','BOD-MDE'),('Barranquilla','BOD-MDE'),('Cartagena','BOD-MDE'),
                      ('Bogotá','BOD-BOG'),('Bucaramanga','BOD-BOG'),('Cali','BOD-BOG')) AS b(ciudad, bodega_id) ON b.ciudad = p.ciudad
         WHERE p.estado = 'Pendiente de despacho' AND p.fecha <= centinela.fecha_corte() GROUP BY 1, 2)
SELECT i.sku, pr.nombre, pr.linea, pr.clase_abc, i.bodega_id, i.existencia_final AS existencia,
       round(dem.demanda_prom_30d, 1) AS demanda_prom_30d,
       round(i.existencia_final / nullif(dem.demanda_prom_30d, 0), 1) AS cobertura_dias,
       coalesce(pend.unidades_pendientes, 0) AS unidades_pendientes
FROM centinela.inventario_diario i JOIN centinela.productos pr USING (sku) JOIN dem USING (sku, bodega_id)
LEFT JOIN pend USING (sku, bodega_id) WHERE i.fecha = centinela.fecha_corte();

-- v_cartera_cliente ya filtra facturas y pagos por fecha_corte().
-- v_actividad_cliente ya filtra pedidos por fecha_corte().
-- Ambas mantienen su definición oficial.
