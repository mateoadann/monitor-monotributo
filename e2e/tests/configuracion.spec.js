const { test, expect } = require('@playwright/test');
const {
  login,
  navigateToTab,
  confirmDialog,
  getFlashMessage,
  waitForNavigation,
} = require('./helpers');

/** Helper: fill a date input made readonly by AirDatepicker via JS */
async function fillDateInput(page, name, value) {
  await page.evaluate(({ name, value }) => {
    const input = document.querySelector(`input[name="${name}"]`);
    input.removeAttribute('readonly');
    input.value = value;
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }, { name, value });
}

test.describe.serial('Creacion de vigencia', () => {
  test('crear vigencia exitosa', async ({ page }) => {
    await login(page);
    await navigateToTab(page, 'configuracion');

    // Cleanup: if a previous test left a 01/01/2024 vigencia, delete it first
    // Dates display as DD/MM/YYYY in the table
    const existingRow = page.locator('.table__row', { hasText: '01/01/2024' });
    if (await existingRow.count() > 0) {
      await existingRow.locator('button[aria-label="Eliminar"]').click();
      await confirmDialog(page);
      await waitForNavigation(page);
      await navigateToTab(page, 'configuracion');
    }

    await fillDateInput(page, 'vigencia_desde', '2024-01-01');
    await fillDateInput(page, 'vigencia_hasta', '2024-12-31');

    await page.click('button:has-text("Crear vigencia")');
    await confirmDialog(page);

    // Check flash before networkidle (flash auto-hides after 2s)
    const flash = await getFlashMessage(page, 'success');
    expect(flash).toContain('Vigencia creada');

    // Table uses div.table__row, not tr — dates display as DD/MM/YYYY
    const panel = page.locator('#configuracion');
    await expect(panel.locator('text=01/01/2024')).toBeVisible();
  });

  test('crear vigencia sin fecha desde (validacion HTML + server-side)', async ({ page }) => {
    await login(page);
    await navigateToTab(page, 'configuracion');

    // HTML required validation prevents submit
    const desdeInput = page.locator('input[name="vigencia_desde"]');
    await expect(desdeInput).toHaveAttribute('required', '');

    // Remove required attribute via JS to test server-side validation
    await page.evaluate(() => {
      const input = document.querySelector('input[name="vigencia_desde"]');
      input.removeAttribute('required');
      input.removeAttribute('readonly');
    });

    // Clear the value to ensure it's empty
    await page.evaluate(() => {
      document.querySelector('input[name="vigencia_desde"]').value = '';
    });

    await page.click('button:has-text("Crear vigencia")');
    await confirmDialog(page);
    await waitForNavigation(page);

    const flash = await getFlashMessage(page, 'error');
    expect(flash).toContain('Completa los campos requeridos');
  });

  test('crear vigencia con fechas invertidas', async ({ page }) => {
    await login(page);
    await navigateToTab(page, 'configuracion');

    await fillDateInput(page, 'vigencia_desde', '2023-12-31');
    await fillDateInput(page, 'vigencia_hasta', '2023-01-01');

    await page.click('button:has-text("Crear vigencia")');
    await confirmDialog(page);
    await waitForNavigation(page);

    const flash = await getFlashMessage(page, 'error');
    expect(flash).toContain('vigencia hasta debe ser posterior');
  });

  test('crear vigencia superpuesta', async ({ page }) => {
    await login(page);
    await navigateToTab(page, 'configuracion');

    await fillDateInput(page, 'vigencia_desde', '2025-06-01');

    await page.click('button:has-text("Crear vigencia")');
    await confirmDialog(page);
    await waitForNavigation(page);

    const flash = await getFlashMessage(page, 'error');
    expect(flash).toContain('se superpone');
  });
});

test.describe.serial('Insertar topes a categorias', () => {
  test('ir a editar vigencia creada', async ({ page }) => {
    await login(page);
    await navigateToTab(page, 'configuracion');

    // Table uses div.table__row, not tr
    const row = page.locator('.table__row', { hasText: '01/01/2024' });
    await expect(row).toBeVisible();

    // Edit link is an icon with aria-label="Editar"
    await row.locator('a[aria-label="Editar"]').click();
    await waitForNavigation(page);

    await expect(page).toHaveURL(/\/vigencias\/\d+\/edit/);
  });

  test('insertar topes a todas las categorias', async ({ page }) => {
    await login(page);
    await navigateToTab(page, 'configuracion');

    const row = page.locator('.table__row', { hasText: '01/01/2024' });
    await row.locator('a[aria-label="Editar"]').click();
    await waitForNavigation(page);

    const topeInputs = page.locator('input[name^="tope_"]');
    const count = await topeInputs.count();
    expect(count).toBe(11);

    for (let i = 0; i < count; i++) {
      const value = `${(i + 1) * 10}.000.000,00`;
      await topeInputs.nth(i).fill(value);
    }

    await page.click('button:has-text("Guardar topes")');
    await confirmDialog(page);

    // Check flash before networkidle (flash auto-hides after 2s)
    const flash = await getFlashMessage(page, 'success');
    expect(flash).toContain('Topes actualizados');
  });
});

test.describe.serial('Editar topes de categorias', () => {
  test('editar topes exitoso', async ({ page }) => {
    await login(page);
    await navigateToTab(page, 'configuracion');

    const row = page.locator('.table__row', { hasText: '01/01/2024' });
    await row.locator('a[aria-label="Editar"]').click();
    await waitForNavigation(page);

    const firstTope = page.locator('input[name^="tope_"]').first();
    await firstTope.fill('99.999.999,99');

    await page.click('button:has-text("Guardar topes")');
    await confirmDialog(page);

    // Check flash before networkidle (flash auto-hides after 2s)
    const flash = await getFlashMessage(page, 'success');
    expect(flash).toContain('Topes actualizados');

    await waitForNavigation(page);
    const updatedValue = await page.locator('input[name^="tope_"]').first().inputValue();
    expect(updatedValue).toContain('99.999.999,99');
  });

  test('editar tope con valor invalido', async ({ page }) => {
    await login(page);
    await navigateToTab(page, 'configuracion');

    const row = page.locator('.table__row', { hasText: '01/01/2024' });
    await row.locator('a[aria-label="Editar"]').click();
    await waitForNavigation(page);

    const firstTope = page.locator('input[name^="tope_"]').first();
    await firstTope.fill('abc');

    await page.click('button:has-text("Guardar topes")');
    await confirmDialog(page);

    // Error flashes don't auto-hide, so waitForNavigation is safe
    await waitForNavigation(page);
    const flash = await getFlashMessage(page, 'error');
    expect(flash).toBeTruthy();
  });
});

test.describe.serial('Cleanup', () => {
  test('eliminar vigencia de prueba', async ({ page }) => {
    await login(page);
    await navigateToTab(page, 'configuracion');

    const row = page.locator('.table__row', { hasText: '01/01/2024' });
    await expect(row).toBeVisible();

    // Delete button is an icon with aria-label="Eliminar" inside a form
    await row.locator('button[aria-label="Eliminar"]').click();
    await confirmDialog(page);
    await waitForNavigation(page);

    await expect(page.locator('.table__row', { hasText: '01/01/2024' })).toHaveCount(0);
  });
});
