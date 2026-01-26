import re
from playwright.sync_api import Playwright, sync_playwright, expect


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://www.afip.gob.ar/landing/default.asp")
    with page.expect_popup() as page1_info:
        page.get_by_role("link", name="Iniciar sesión").click()
    page1 = page1_info.value
    page1.get_by_role("spinbutton").fill("20442030147")
    page1.get_by_role("button", name="Siguiente").click()
    page1.get_by_role("button", name="Ingresar").click()
    page1.get_by_role("combobox", name="Buscador").click()

    # Se busca el servicio "Comprobantes en línea"
    page1.get_by_role("combobox", name="Buscador").fill("Comprobantes en línea")
    # Luego debe esperar a que carge el nombre del servicio en el listado y hacer click.
    # Para evitar problemas puede escribrir letra por letra. De esta manera se asegura que el
    # servicio aparezca en el listado.
    
    
    # Se ingresa al servicio "Comprobantes en línea"
    with page1.expect_popup() as page2_info:
        page1.get_by_role("link", name="Comprobantes en línea Sistema").click()
    page2 = page2_info.value
    
    # Esta celda indica el nombre del monotributista asociado al CUIT ingresado
    page2.get_by_role("cell", name="20442030147 - ADAN MATEO", exact=True).click()
    # El nombre del monotributista aparece en este boton, extraido de la celda anterior.
    page2.get_by_role("button", name="ADAN MATEO").click()
    
    # Ir a la seccion de consultas de comprobantes.
    page2.get_by_role("button", name="Consultas").click()
    
    # Rellenar el campo "Desde" con la fecha deseada.
    page2.get_by_role("textbox", name="Desde").fill("24/12/2025")
    # Rellenar el campo "Hasta" con la fecha deseada.
    page2.get_by_role("textbox", name="Hasta").fill("24/01/2026")
    
    # Seleccionar los tipos de comprobante deseados.
    page2.get_by_role("combobox", name="Tipo de Comprobante").click()
    page2.locator("select[name=\"idTipoComprobante\"]").select_option("11") # Para factura C
    page2.locator("select[name=\"idTipoComprobante\"]").select_option("13") # Para nota de credito C



    
    # Buscar los comprobantes con los filtros aplicados.
    page2.get_by_role("button", name="Buscar").click()
    
    # Esta es la columna de la tabla de resultados que contiene el punto de venta + nro de comprobante.
    page2.get_by_role("columnheader", name="Nro. Comprobante").click()
    
    
    # Como cerrar la sesión del monotributista correctamente.
    page2.get_by_role("button", name="< Volver").click()
    page2.once("dialog", lambda dialog: dialog.dismiss())
    page2.get_by_role("link", name="Salir").click()
    page2.close()
    page1.locator("#userIconoChico").click()
    page1.get_by_role("button", name=" Cerrar sesión").click()
    page1.close()

    # ---------------------
    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
