const { test, expect } = require('@playwright/test');
const { login } = require('./helpers');

test.describe('Login', () => {
  test('login exitoso redirige al dashboard', async ({ page }) => {
    await page.goto('/login');
    await page.fill('input[name="username"]', 'admin');
    await page.fill('input[name="password"]', 'admin');
    await page.click('button[type="submit"]');

    await page.waitForURL('**/');
    await expect(page).toHaveURL('/');
    await expect(page.locator('text=Monitor Monotributo')).toBeVisible();
  });

  test('login con credenciales incorrectas muestra error', async ({ page }) => {
    await page.goto('/login');
    await page.fill('input[name="username"]', 'admin');
    await page.fill('input[name="password"]', 'wrongpassword');
    await page.click('button[type="submit"]');

    await expect(page.locator('div.alert')).toContainText('Usuario o contrasena incorrecta');
    await expect(page).toHaveURL(/\/login/);
  });

  test('login con campos vacios no navega', async ({ page }) => {
    await page.goto('/login');
    await page.click('button[type="submit"]');

    await expect(page).toHaveURL(/\/login/);
  });

  test('acceso protegido redirige a login', async ({ page }) => {
    await page.goto('/');

    await expect(page).toHaveURL(/\/login/);
  });

  test('logout redirige a login', async ({ page }) => {
    await login(page);
    await expect(page).toHaveURL('/');

    await page.click('.user-dropdown__trigger');
    await page.waitForSelector('.user-dropdown__menu.is-open');
    await page.click('.user-dropdown__item--danger');

    await expect(page).toHaveURL(/\/login/);
  });
});
