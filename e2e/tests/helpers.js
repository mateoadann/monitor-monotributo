/**
 * Login helper - navigates to login and authenticates
 */
async function login(page, username = 'admin', password = 'admin') {
  await page.goto('/login');
  await page.fill('input[name="username"]', username);
  await page.fill('input[name="password"]', password);
  await page.click('button[type="submit"]');
  await page.waitForURL('**/');
}

/**
 * Navigate to a specific tab
 */
async function navigateToTab(page, tabName) {
  const tabButton = page.locator(`.tab[data-tab="${tabName}"]`);
  await tabButton.click();
  // Tab switch can be client-side (just class toggle) or cause a page reload
  // (when switching away from monotributistas with mono_anchor in URL)
  await page.waitForSelector(`.tabs__panel.is-active#${tabName}`, { timeout: 10000 });
}

/**
 * Open a modal by name
 */
async function openModal(page, modalName) {
  await page.click(`[data-open="${modalName}"]`);
  await page.waitForSelector(`[data-modal="${modalName}"].is-open`);
}

/**
 * Close the currently open modal
 */
async function closeModal(page, modalName) {
  await page.click(`[data-modal="${modalName}"] [data-close]`);
  await page.waitForSelector(`[data-modal="${modalName}"].is-open`, { state: 'detached' }).catch(() => {});
  // Wait for modal to fully close
  await page.waitForTimeout(300);
}

/**
 * Confirm a data-confirm dialog
 */
async function confirmDialog(page) {
  await page.waitForSelector('#confirmModal.is-open');
  await page.click('#confirmOk');
}

/**
 * Dismiss a data-confirm dialog
 */
async function dismissDialog(page) {
  await page.waitForSelector('#confirmModal.is-open');
  await page.click('#confirmModal button[data-close]');
}

/**
 * Get flash message text
 */
async function getFlashMessage(page, type = 'success') {
  const selector = `.flash--${type}`;
  await page.waitForSelector(selector, { timeout: 5000 });
  return page.textContent(selector);
}

/**
 * Get modal flash message text
 */
async function getModalFlashMessage(page) {
  const selector = '.flash--error.flash--modal';
  await page.waitForSelector(selector, { timeout: 5000 });
  return page.textContent(selector);
}

/**
 * Wait for page reload after form submission
 */
async function waitForNavigation(page) {
  await page.waitForLoadState('networkidle');
}

/**
 * Fill the AirDatepicker month-year input
 * AirDatepicker inputs don't accept regular fill() — need to click and select
 */
async function fillDateInput(page, selector, dateStr) {
  // Clear and type directly — AirDatepicker accepts typed input
  const input = page.locator(selector);
  await input.click();
  await input.fill(dateStr);
  // Press Enter or click away to close picker
  await input.press('Enter');
}

module.exports = {
  login,
  navigateToTab,
  openModal,
  closeModal,
  confirmDialog,
  dismissDialog,
  getFlashMessage,
  getModalFlashMessage,
  waitForNavigation,
  fillDateInput,
};
