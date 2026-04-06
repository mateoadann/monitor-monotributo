const { test, expect } = require('@playwright/test');
const { login, waitForNavigation } = require('./helpers');

test.describe('Cambio de monotributista', () => {
  test('cargar tab calculo con Amor Luis', async ({ page }) => {
    await login(page);
    await page.goto('/?tab=calculo&monotributista=3');
    await waitForNavigation(page);

    const trigger = page.locator('[data-search-select="calculo"] [data-select-trigger]');
    await expect(trigger).toContainText('Amor Luis');

    const monthCards = page.locator('.month-card');
    await expect(monthCards).toHaveCount(12);
  });

  test('cambiar a Garcia Maria', async ({ page }) => {
    await login(page);
    await page.goto('/?tab=calculo&monotributista=3');
    await waitForNavigation(page);

    const trigger = page.locator('[data-search-select="calculo"] [data-select-trigger]');
    await trigger.click();

    const checkbox = page.locator('[data-search-select="calculo"]').getByText('Garcia Maria');
    await checkbox.click();

    await page.waitForURL(/monotributista=4/);
    await waitForNavigation(page);

    await expect(trigger).toContainText('Garcia Maria');
  });
});

test.describe('Verificacion de campos - Caso SUBE (Amor Luis)', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
    await page.goto('/?tab=calculo&monotributista=3');
    await waitForNavigation(page);
  });

  test('categoria actual correcta', async ({ page }) => {
    const catActual = page.locator('.summary-card').filter({ hasText: 'Categoria actual' });
    await expect(catActual).toBeVisible();
    await expect(catActual).toContainText('B');
  });

  test('categoria corresponde correcta', async ({ page }) => {
    const catCorresponde = page.locator('.summary-card').filter({ hasText: 'Categoria corresponde' });
    await expect(catCorresponde).toBeVisible();
    await expect(catCorresponde).toContainText('C');
  });

  test('total 12 meses', async ({ page }) => {
    const total = page.locator('.summary-card').filter({ hasText: 'Total 12 meses' });
    await expect(total).toBeVisible();
    await expect(total).toContainText(/\$\s*[\d.]+,\d{2}/);
  });

  test('margen de exclusion', async ({ page }) => {
    const margenExclusion = page.locator('.metric-card').filter({ hasText: 'Margen de exclusion' });
    await expect(margenExclusion).toBeVisible();
    await expect(margenExclusion).toContainText(/\$\s*[\d.]+,\d{2}/);
  });

  test('margen de categoria', async ({ page }) => {
    // In dev, labels do NOT have "(B)" — just "Margen de categoria" (without "siguiente")
    const margenCat = page.locator('.metric-card').filter({ hasText: 'Margen de categoria' }).filter({ hasNot: page.locator('text=siguiente') });
    await expect(margenCat).toBeVisible();
    await expect(margenCat).toContainText(/-?\$\s*[\d.]+,\d{2}/);
  });

  test('margen de siguiente categoria', async ({ page }) => {
    const margenSiguiente = page.locator('.metric-card').filter({ hasText: 'siguiente' });
    await expect(margenSiguiente).toBeVisible();
    await expect(margenSiguiente).toContainText(/\$\s*[\d.]+,\d{2}/);
  });
});

test.describe('Verificacion de campos - Caso BAJA (Garcia Maria)', () => {
  test.beforeEach(async ({ page }) => {
    await login(page);
    await page.goto('/?tab=calculo&monotributista=4');
    await waitForNavigation(page);
  });

  test('categoria actual correcta', async ({ page }) => {
    const catActual = page.locator('.summary-card').filter({ hasText: 'Categoria actual' });
    await expect(catActual).toBeVisible();
    await expect(catActual).toContainText('C');
  });

  test('categoria corresponde correcta', async ({ page }) => {
    const catCorresponde = page.locator('.summary-card').filter({ hasText: 'Categoria corresponde' });
    await expect(catCorresponde).toBeVisible();
    await expect(catCorresponde).toContainText('A');
  });

  test('total 12 meses', async ({ page }) => {
    const total = page.locator('.summary-card').filter({ hasText: 'Total 12 meses' });
    await expect(total).toBeVisible();
    await expect(total).toContainText(/\$\s*[\d.]+,\d{2}/);
  });

  test('margen de exclusion', async ({ page }) => {
    const margenExclusion = page.locator('.metric-card').filter({ hasText: 'Margen de exclusion' });
    await expect(margenExclusion).toBeVisible();
    await expect(margenExclusion).toContainText(/\$\s*[\d.]+,\d{2}/);
  });

  test('margen de categoria', async ({ page }) => {
    // In dev, labels do NOT have "(C)" — just "Margen de categoria" (without "siguiente")
    const margenCat = page.locator('.metric-card').filter({ hasText: 'Margen de categoria' }).filter({ hasNot: page.locator('text=siguiente') });
    await expect(margenCat).toBeVisible();
    await expect(margenCat).toContainText(/\$\s*[\d.]+,\d{2}/);
  });

  test('no muestra margen de siguiente categoria (caso BAJA)', async ({ page }) => {
    // In BAJA case, there is no "siguiente" card in dev
    const margenSiguiente = page.locator('.metric-card').filter({ hasText: 'siguiente' });
    await expect(margenSiguiente).toHaveCount(0);
  });
});

test.describe('Cambio de periodo', () => {
  test('cambiar periodo actualiza la URL con anchor', async ({ page }) => {
    await login(page);
    await page.goto('/?tab=calculo&monotributista=3&anchor=2026-04');
    await waitForNavigation(page);

    // The period input is a readonly AirDatepicker text input.
    // Change the hidden input value directly and submit the form.
    await page.evaluate(() => {
      document.querySelector('#anchorMonthValue').value = '2025-06';
    });
    await page.locator('#calculo').getByRole('button', { name: 'Ver periodo' }).click();
    await page.waitForLoadState('networkidle');
    await expect(page).toHaveURL(/anchor=2025-06/);
  });
});
