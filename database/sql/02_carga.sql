-- Ejecutar con psql desde la carpeta datos/:  psql -d <base> -f sql/02_carga.sql
SET search_path TO centinela;
\copy vendedores (vendedor_id, nombre, region) FROM 'csv/vendedores.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8');
\copy clientes (cliente_id, nombre, segmento, ciudad, region, vendedor_id, plazo_dias, cupo_credito, fecha_alta) FROM 'csv/clientes.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8');
\copy proveedores (proveedor_id, nombre, lead_time_dias, pais) FROM 'csv/proveedores.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8');
\copy productos (sku, nombre, linea, proveedor_id, clase_abc, unidad) FROM 'csv/productos.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8');
\copy bodegas (bodega_id, nombre, ciudad) FROM 'csv/bodegas.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8');
\copy lista_precios (sku, fecha_vigencia, precio_lista) FROM 'csv/lista_precios.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8');
\copy costos_proveedor (sku, proveedor_id, fecha_vigencia, costo_unitario) FROM 'csv/costos_proveedor.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8');
\copy ordenes_compra (oc_id, proveedor_id, sku, bodega_id, fecha_oc, fecha_esperada, fecha_recibida, cantidad, costo_unitario, estado) FROM 'csv/ordenes_compra.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8');
\copy pedidos (pedido_id, fecha, cliente_id, vendedor_id, ciudad, canal, estado) FROM 'csv/pedidos.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8');
\copy pedidos_detalle (pedido_id, linea_n, sku, cantidad, precio_lista, precio_unitario, descuento_pct, aprobacion_especial, valor_neto, costo_unitario) FROM 'csv/pedidos_detalle.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8');
\copy facturas (factura_id, pedido_id, cliente_id, fecha_factura, fecha_vencimiento, valor_neto, iva, valor_total) FROM 'csv/facturas.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8');
\copy pagos (pago_id, factura_id, fecha_pago, valor, medio_pago) FROM 'csv/pagos.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8');
\copy inventario_diario (fecha, bodega_id, sku, existencia_inicial, entradas, salidas, existencia_final) FROM 'csv/inventario_diario.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8');
\copy ref_topes_descuento (segmento, tope_descuento_pct) FROM 'csv/ref_topes_descuento.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8');
\copy ref_margen_minimo_linea (linea, margen_minimo_pct) FROM 'csv/ref_margen_minimo_linea.csv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8');
