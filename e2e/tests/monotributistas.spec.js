const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');
const {
  login,
  navigateToTab,
  openModal,
  confirmDialog,
  getFlashMessage,
  getModalFlashMessage,
  waitForNavigation,
} = require('./helpers');

const TAB = 'monotributistas';

test.describe.serial('Carga de monotributista', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
    await navigateToTab(page, TAB);
  });

  test('Crear monotributista exitoso', async ({ page }) => {
    // Use a unique CUIT based on timestamp to avoid duplicates from previous runs
    const uniqueSuffix = String(Date.now()).slice(-7);
    const testCuit = `2044${uniqueSuffix}`;

    await openModal(page, 'monotributista');

    await page.fill('input[name="razon_social"]', 'Test Perez');
    await page.fill('input[name="cuit"]', testCuit);
    await page.fill('input[name="clave_fiscal"]', 'clave123');
    await page.selectOption('select[name="categoria_actual_id"]', { label: 'C' });

    await page.click('button:has-text("Crear monotributista")');
    await confirmDialog(page);

    // Check flash before it auto-hides (2s)
    const flash = await getFlashMessage(page, 'success');
    expect(flash).toBeTruthy();

    await page.waitForLoadState('networkidle');
    await navigateToTab(page, TAB);
    // Search by unique CUIT to bypass pagination and avoid multiple "Test Perez" from previous runs
    await page.fill('#monoSearch', testCuit);
    await page.locator('#monoSearch').dispatchEvent('input');
    await page.waitForTimeout(500);
    const row = page.locator(`[data-row="mono"][data-cuit="${testCuit}"]`);
    await expect(row).toBeVisible();
  });

  test('Crear monotributista con campos vacios (validacion server-side)', async ({ page }) => {
    await openModal(page, 'monotributista');

    // Remove required attributes to bypass HTML validation
    await page.evaluate(() => {
      document.querySelectorAll('[data-modal="monotributista"] input[required], [data-modal="monotributista"] select[required]').forEach(el => {
        el.removeAttribute('required');
      });
    });

    // Leave razon_social empty, fill the rest
    await page.fill('input[name="cuit"]', '20999888777');
    await page.fill('input[name="clave_fiscal"]', 'clave123');
    await page.selectOption('select[name="categoria_actual_id"]', { label: 'C' });

    await page.click('button:has-text("Crear monotributista")');
    await confirmDialog(page);

    // Modal flash auto-hides after 4s — check before networkidle
    const flash = await getModalFlashMessage(page);
    expect(flash).toContain('Completa los campos requeridos');
  });

  test('Crear monotributista con CUIT invalido', async ({ page }) => {
    await openModal(page, 'monotributista');

    await page.fill('input[name="razon_social"]', 'CUIT Invalido');
    await page.fill('input[name="clave_fiscal"]', 'clave123');
    await page.selectOption('select[name="categoria_actual_id"]', { label: 'C' });

    // Fill CUIT with invalid value (not 11 digits)
    await page.evaluate(() => {
      const cuitInput = document.querySelector('input[name="cuit"]');
      cuitInput.removeAttribute('required');
      cuitInput.removeAttribute('pattern');
      cuitInput.removeAttribute('minlength');
      cuitInput.removeAttribute('maxlength');
    });
    await page.fill('input[name="cuit"]', '123');

    await page.click('button:has-text("Crear monotributista")');
    await confirmDialog(page);

    // Modal flash auto-hides after 4s — check before networkidle
    const flash = await getModalFlashMessage(page);
    expect(flash).toContain('El CUIT debe tener 11 digitos');
  });

  test('Crear monotributista con CUIT duplicado', async ({ page }) => {
    await openModal(page, 'monotributista');

    await page.fill('input[name="razon_social"]', 'Duplicado Test');
    await page.fill('input[name="cuit"]', '20304050607'); // Amor Luis's CUIT
    await page.fill('input[name="clave_fiscal"]', 'clave123');
    await page.selectOption('select[name="categoria_actual_id"]', { label: 'C' });

    await page.click('button:has-text("Crear monotributista")');
    await confirmDialog(page);

    // Modal flash auto-hides after 4s — check before networkidle
    const flash = await getModalFlashMessage(page);
    expect(flash).toContain('El CUIT ingresado ya existe');
  });
});

test.describe.serial('Busqueda y filtro', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
    await navigateToTab(page, TAB);
  });

  test('Buscar monotributista por nombre', async ({ page }) => {
    await page.fill('#monoSearch', 'Amor');
    await page.waitForTimeout(300);

    // data-razon stores lowercase
    const amorRow = page.locator('[data-row="mono"][data-razon="amor luis"]');
    await expect(amorRow).toBeVisible();

    const garciaRow = page.locator('[data-row="mono"][data-razon="garcia maria"]');
    await expect(garciaRow).toBeHidden();
  });

  test('Buscar por CUIT', async ({ page }) => {
    await page.fill('#monoSearch', '20112233445');
    await page.waitForTimeout(300);

    const garciaRow = page.locator('[data-row="mono"][data-razon="garcia maria"]');
    await expect(garciaRow).toBeVisible();

    const amorRow = page.locator('[data-row="mono"][data-razon="amor luis"]');
    await expect(amorRow).toBeHidden();
  });

  test('Filtrar por categoria', async ({ page }) => {
    await page.selectOption('#monoCategoria', 'B');
    await page.waitForTimeout(300);

    const amorRow = page.locator('[data-row="mono"][data-razon="amor luis"]');
    await expect(amorRow).toBeVisible();

    // Garcia Maria is Cat C, should be hidden
    const garciaRow = page.locator('[data-row="mono"][data-razon="garcia maria"]');
    await expect(garciaRow).toBeHidden();

    // Reset filter
    await page.selectOption('#monoCategoria', '');
    await page.waitForTimeout(300);

    // After reset, Amor Luis should be visible (page 1 alphabetically)
    await expect(amorRow).toBeVisible();
    // Garcia Maria may be on a later page due to pagination (page size = 10)
    // Verify she appears when searched
    await page.fill('#monoSearch', 'Garcia Maria');
    await page.waitForTimeout(300);
    await expect(garciaRow).toBeVisible();
    // Clear search for next tests
    await page.fill('#monoSearch', '');
    await page.waitForTimeout(300);
  });

  test('Limpiar busqueda muestra todos', async ({ page }) => {
    await page.fill('#monoSearch', 'Amor');
    await page.locator('#monoSearch').dispatchEvent('input');
    await page.waitForTimeout(300);

    const garciaRow = page.locator('[data-row="mono"][data-razon="garcia maria"]');
    await expect(garciaRow).toBeHidden();

    // Clear the search
    await page.fill('#monoSearch', '');
    await page.locator('#monoSearch').dispatchEvent('input');
    await page.waitForTimeout(300);

    // After clearing, pagination shows page 1 (10 rows alphabetically).
    // "Amor Luis" is near the top alphabetically so should be on page 1.
    const amorRow = page.locator('[data-row="mono"][data-razon="amor luis"]');
    await expect(amorRow).toBeVisible();

    // "Garcia Maria" may be beyond page 1 due to many test monotributistas.
    // Verify she appears when searched directly.
    await page.fill('#monoSearch', 'Garcia Maria');
    await page.locator('#monoSearch').dispatchEvent('input');
    await page.waitForTimeout(300);
    await expect(garciaRow).toBeVisible();

    // Clear search for next tests
    await page.fill('#monoSearch', '');
    await page.locator('#monoSearch').dispatchEvent('input');
    await page.waitForTimeout(300);
  });
});

test.describe.serial('Edicion de monotributista', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
    await navigateToTab(page, TAB);
  });

  test('Editar monotributista exitoso', async ({ page }) => {
    // Find a Test Perez — search and pick the first visible row on page 1
    await page.fill('#monoSearch', 'Test Perez');
    await page.locator('#monoSearch').dispatchEvent('input');
    await page.waitForTimeout(500);

    // Multiple "Test Perez" entries may exist from previous runs.
    // Find the CUIT of the first visible one to target precisely.
    const visibleCuit = await page.evaluate(() => {
      const rows = document.querySelectorAll('[data-row="mono"]');
      for (const row of rows) {
        if (row.style.display !== 'none' && row.dataset.razon && row.dataset.razon.includes('test perez')) {
          return row.dataset.cuit;
        }
      }
      return null;
    });
    expect(visibleCuit).toBeTruthy();
    const row = page.locator(`[data-row="mono"][data-cuit="${visibleCuit}"]`);
    await expect(row).toBeVisible();

    // Navigate to the edit page by reading the href directly (avoids click-interception issues)
    const editHref = await row.locator('a[aria-label="Editar"]').getAttribute('href');
    expect(editHref).toBeTruthy();
    await page.goto(editHref);
    await page.waitForURL(/\/monotributistas\/\d+\/edit/, { timeout: 10000 });
    await page.waitForLoadState('networkidle');

    // Change razon_social
    await page.fill('input[name="razon_social"]', 'Test Perez Modificado');

    await page.click('button:has-text("Guardar cambios")');
    await confirmDialog(page);

    // Check flash before it auto-hides
    const flash = await getFlashMessage(page, 'success');
    expect(flash).toBeTruthy();

    // Should redirect to dashboard with monotributistas tab
    await page.waitForLoadState('networkidle');
    await expect(page).toHaveURL(/tab=monotributistas|\/$/);

    // Verify the name changed — navigate to monotributistas tab if not already there
    await navigateToTab(page, TAB);
    // Search by CUIT to find the specific edited row (avoids strict mode with duplicates)
    await page.fill('#monoSearch', visibleCuit);
    await page.locator('#monoSearch').dispatchEvent('input');
    await page.waitForTimeout(500);
    const updatedRow = page.locator(`[data-row="mono"][data-cuit="${visibleCuit}"]`);
    await expect(updatedRow).toBeVisible();
    // Verify the name was actually changed
    const updatedRazon = await updatedRow.getAttribute('data-razon');
    expect(updatedRazon).toBe('test perez modificado');
  });

  test('Editar con CUIT invalido', async ({ page }) => {
    await page.fill('#monoSearch', 'Test Perez');
    await page.locator('#monoSearch').dispatchEvent('input');
    await page.waitForTimeout(500);

    // Find the first visible "test perez" row
    const visibleCuit = await page.evaluate(() => {
      const rows = document.querySelectorAll('[data-row="mono"]');
      for (const row of rows) {
        if (row.style.display !== 'none' && row.dataset.razon && row.dataset.razon.includes('test perez')) {
          return row.dataset.cuit;
        }
      }
      return null;
    });
    const row = page.locator(`[data-row="mono"][data-cuit="${visibleCuit}"]`);
    // Navigate to the edit page by reading the href directly
    const editHref = await row.locator('a[aria-label="Editar"]').getAttribute('href');
    expect(editHref).toBeTruthy();
    await page.goto(editHref);
    await page.waitForURL(/\/monotributistas\/\d+\/edit/, { timeout: 10000 });
    await page.waitForLoadState('networkidle');

    // Remove HTML validation attrs on CUIT
    await page.evaluate(() => {
      const cuitInput = document.querySelector('input[name="cuit"]');
      cuitInput.removeAttribute('required');
      cuitInput.removeAttribute('pattern');
      cuitInput.removeAttribute('minlength');
      cuitInput.removeAttribute('maxlength');
    });
    await page.fill('input[name="cuit"]', '123');

    await page.click('button:has-text("Guardar cambios")');
    await confirmDialog(page);
    await page.waitForLoadState('networkidle');

    // Error flash on edit page uses flash--modal class (auto-hides after 4s)
    const flash = await getModalFlashMessage(page);
    expect(flash).toContain('El CUIT debe tener 11 digitos');
  });
});

test.describe('Filtrar por categoria', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
    await navigateToTab(page, TAB);
  });

  test('Filtrar tabla por categoria B', async ({ page }) => {
    await page.selectOption('#monoCategoria', 'B');
    await page.waitForTimeout(300);

    const visibleRows = page.locator('[data-row="mono"]:visible');
    const count = await visibleRows.count();
    expect(count).toBeGreaterThan(0);

    // All visible rows should be category B
    for (let i = 0; i < count; i++) {
      const cat = await visibleRows.nth(i).getAttribute('data-categoria');
      expect(cat).toBe('B');
    }
  });

  test('Filtrar tabla por categoria C', async ({ page }) => {
    await page.selectOption('#monoCategoria', 'C');
    await page.waitForTimeout(300);

    const visibleRows = page.locator('[data-row="mono"]:visible');
    const count = await visibleRows.count();
    expect(count).toBeGreaterThan(0);

    for (let i = 0; i < count; i++) {
      const cat = await visibleRows.nth(i).getAttribute('data-categoria');
      expect(cat).toBe('C');
    }
  });
});

test.describe('Periodo de corte', () => {
  test('Modificar periodo de corte', async ({ page }) => {
    await login(page);
    await navigateToTab(page, TAB);

    // Calculate previous month in YYYY-MM format
    const now = new Date();
    now.setMonth(now.getMonth() - 1);
    const year = now.getFullYear();
    const month = String(now.getMonth() + 1).padStart(2, '0');
    const periodoAnterior = `${year}-${month}`;

    // The period input is a readonly AirDatepicker text input.
    // Change the hidden input value directly and submit the form.
    await page.evaluate((value) => {
      document.querySelector('#monoAnchorValue').value = value;
    }, periodoAnterior);

    const panel = page.locator('#monotributistas');
    await panel.locator('button:has-text("Ver periodo")').click();
    await page.waitForLoadState('networkidle');

    // Verify URL contains the period anchor
    await expect(page).toHaveURL(new RegExp(`mono_anchor=${periodoAnterior}`));
  });
});

test.describe.serial('Paginacion', () => {
  const csvPath = path.join(__dirname, 'tmp_pagination.csv');

  test.afterAll(async () => {
    // Clean up temp CSV file
    if (fs.existsSync(csvPath)) {
      fs.unlinkSync(csvPath);
    }
  });

  test('Paginacion funciona correctamente', async ({ page }) => {
    await login(page);
    await navigateToTab(page, TAB);

    // Create 10 monotributistas via CSV to ensure > 10 total
    // Use timestamp-based CUITs to avoid conflicts with previous runs
    const ts = String(Date.now()).slice(-5);
    const csvLines = ['razon_social,cuit,clave_fiscal,categoria_actual'];
    for (let i = 1; i <= 10; i++) {
      const cuit = `27${ts}${String(i).padStart(4, '0')}`;
      csvLines.push(`Paginacion Test ${i},${cuit},clave${i},A`);
    }
    fs.writeFileSync(csvPath, csvLines.join('\n'));

    await openModal(page, 'monotributistas-import');

    // Uncheck dry_run
    const dryRunCheckbox = page.locator('input[name="dry_run"]');
    if (await dryRunCheckbox.isChecked()) {
      await dryRunCheckbox.uncheck();
    }

    await page.setInputFiles('#monotributistas-csv-input', csvPath);
    await page.click('button:has-text("Procesar importacion")');
    // The form has data-confirm, so we need to confirm
    await confirmDialog(page);

    // Check flash before it auto-hides
    const flash = await getFlashMessage(page, 'success');
    expect(flash).toContain('Importacion finalizada');
    expect(flash).toContain('creados=10');

    await page.waitForLoadState('networkidle');
    // Navigate to monotributistas tab
    await navigateToTab(page, TAB);

    // Check pagination info
    const pagination = page.locator('[data-pagination="monotributistas-top"]');
    const pageInfo = pagination.locator('[data-page-info]');
    await expect(pageInfo).toContainText('1');

    // Next button should be enabled
    const nextBtn = pagination.locator('[data-page="next"]');
    await expect(nextBtn).toBeEnabled();

    // Prev button should be disabled
    const prevBtn = pagination.locator('[data-page="prev"]');
    await expect(prevBtn).toBeDisabled();

    // Go to next page
    await nextBtn.click();
    await page.waitForTimeout(300);

    await expect(pageInfo).toContainText('2');
    await expect(prevBtn).toBeEnabled();
  });
});

test.describe.serial('Importacion CSV', () => {
  const csvSuccessPath = path.join(__dirname, 'tmp_import_success.csv');
  const csvDryRunPath = path.join(__dirname, 'tmp_import_dryrun.csv');
  const csvErrorPath = path.join(__dirname, 'tmp_import_error.csv');

  test.afterAll(async () => {
    [csvSuccessPath, csvDryRunPath, csvErrorPath].forEach(f => {
      if (fs.existsSync(f)) fs.unlinkSync(f);
    });
  });

  test.beforeEach(async ({ page }) => {
    await login(page);
    await navigateToTab(page, TAB);
  });

  test('Importar CSV exitoso', async ({ page }) => {
    // Use timestamp-based CUITs to avoid conflicts with previous runs
    const ts = String(Date.now()).slice(-7);
    const csv = [
      'razon_social,cuit,clave_fiscal,categoria_actual',
      `CSV Importado Uno,20${ts}01,clave1,A`,
      `CSV Importado Dos,20${ts}02,clave2,B`,
      `CSV Importado Tres,20${ts}03,clave3,C`,
    ].join('\n');
    fs.writeFileSync(csvSuccessPath, csv);

    await openModal(page, 'monotributistas-import');

    // Uncheck dry_run
    const dryRunCheckbox = page.locator('input[name="dry_run"]');
    if (await dryRunCheckbox.isChecked()) {
      await dryRunCheckbox.uncheck();
    }

    await page.setInputFiles('#monotributistas-csv-input', csvSuccessPath);
    await page.click('button:has-text("Procesar importacion")');
    // The form has data-confirm
    await confirmDialog(page);

    // Check flash before it auto-hides
    const flash = await getFlashMessage(page, 'success');
    expect(flash).toContain('Importacion finalizada');
    expect(flash).toContain('creados=3');
  });

  test('Importar CSV con dry_run', async ({ page }) => {
    // Use timestamp-based CUITs to ensure they don't exist
    const ts = String(Date.now()).slice(-7);
    const csv = [
      'razon_social,cuit,clave_fiscal,categoria_actual',
      `DryRun Uno,20${ts}11,clave1,A`,
      `DryRun Dos,20${ts}12,clave2,B`,
    ].join('\n');
    fs.writeFileSync(csvDryRunPath, csv);

    await openModal(page, 'monotributistas-import');

    // dry_run should be checked by default
    const dryRunCheckbox = page.locator('input[name="dry_run"]');
    await expect(dryRunCheckbox).toBeChecked();

    await page.setInputFiles('#monotributistas-csv-input', csvDryRunPath);
    await page.click('button:has-text("Procesar importacion")');
    // The form has data-confirm
    await confirmDialog(page);

    // Check flash before it auto-hides
    const flash = await getFlashMessage(page, 'success');
    expect(flash).toContain('Simulacion finalizada');
    expect(flash).toContain('creados=2');

    // Verify they were NOT actually created
    await page.waitForLoadState('networkidle');
    await navigateToTab(page, TAB);
    await page.fill('#monoSearch', 'DryRun');
    await page.waitForTimeout(300);

    const rows = page.locator('[data-row="mono"]:visible');
    await expect(rows).toHaveCount(0);
  });

  test('Importar CSV con errores', async ({ page }) => {
    const csv = [
      'razon_social,cuit,clave_fiscal,categoria_actual',
      'Error Test,123,clave1,A', // Invalid CUIT - not 11 digits
    ].join('\n');
    fs.writeFileSync(csvErrorPath, csv);

    await openModal(page, 'monotributistas-import');

    // Uncheck dry_run
    const dryRunCheckbox = page.locator('input[name="dry_run"]');
    if (await dryRunCheckbox.isChecked()) {
      await dryRunCheckbox.uncheck();
    }

    await page.setInputFiles('#monotributistas-csv-input', csvErrorPath);
    await page.click('button:has-text("Procesar importacion")');
    // The form has data-confirm
    await confirmDialog(page);

    // Check flash before it auto-hides
    const flash = await getFlashMessage(page, 'success');
    expect(flash).toContain('errores=1');
  });
});
