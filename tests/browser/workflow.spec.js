const { test, expect } = require('@playwright/test');

async function login(page, role) {
  await page.locator('#account').click();
  await page.getByRole('button', { name: role, exact: true }).click();
  await expect(page.locator('.dashboard')).toBeVisible();
}
async function logout(page) {
  await page.getByRole('button', { name: 'Выйти' }).click();
  await expect(page.locator('#account')).toContainText('Личный кабинет');
}

test('booking, cancellation, review, moderation, schedule and consultation', async ({ page }) => {
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.goto('/');
  await expect(page.locator('.specialist-card')).toHaveCount(6);
  await login(page, 'Клиент');
  await page.getByRole('link', { name: 'Найти специалиста', exact: true }).first().click();
  await page.locator('.specialist-card').first().getByRole('button', { name: 'Выбрать время' }).click();
  await page.locator('[data-slot]').first().click();
  await page.getByLabel('Адрес выезда').fill('Киров, ул. Ленина, 99');
  await page.getByRole('button', { name: 'Подтвердить запись' }).click();
  await expect(page.locator('.booking-row .confirmed')).toHaveCount(1);
  await page.getByRole('button', { name: 'Отменить запись', exact: true }).click();
  await page.getByRole('button', { name: 'Оставить запись', exact: true }).click();
  await expect(page.locator('.booking-row .confirmed')).toHaveCount(1);
  await page.getByRole('button', { name: 'Отменить запись', exact: true }).click();
  await page.getByRole('button', { name: 'Да, отменить' }).click();
  await expect(page.locator('.booking-row .cancelled')).toHaveCount(1);
  await page.getByRole('button', { name: 'Оставить отзыв' }).click();
  await page.getByLabel('Ваш отзыв').fill('Очень внимательный мастер. Всё исправил быстро и аккуратно.');
  await page.getByRole('button', { name: 'Отправить отзыв' }).click();
  await expect(page.locator('#modal')).not.toBeVisible();
  await page.getByRole('button', { name: 'Мои отзывы', exact: true }).click();
  await expect(page.locator('#dashboard-content')).toContainText('На модерации');
  await logout(page);
  await login(page, 'Администратор');
  await page.getByRole('button', { name: 'Модерация отзывов' }).click();
  await page.getByRole('button', { name: 'Опубликовать', exact: true }).first().click();
  await expect(page.getByRole('button', { name: 'Опубликовать', exact: true }).first()).toBeDisabled();
  await page.getByRole('button', { name: 'Специалисты и услуги' }).click();
  await expect(page.locator('.admin-person')).toHaveCount(6);
  await page.locator('.admin-person').first().getByRole('button', { name: 'Скрыть', exact: true }).click();
  await expect(page.locator('.admin-person').first()).toContainText('Скрыт');
  await page.locator('.admin-person').first().getByRole('button', { name: 'Показать', exact: true }).click();
  await expect(page.locator('.admin-person').first()).toContainText('В каталоге');
  await logout(page);
  await login(page, 'Мастер');
  await page.getByRole('button', { name: 'Мои услуги' }).click();
  await page.getByLabel('Название', { exact: true }).fill('Проверка сантехнического оборудования');
  await page.getByLabel('Стоимость, ₽').fill('1700');
  await page.getByRole('button', { name: 'Добавить услугу' }).click();
  await expect(page.getByRole('heading', { name: 'Проверка сантехнического оборудования' })).toBeVisible();
  await page.getByRole('button', { name: 'Моё расписание' }).click();
  const future = new Date(Date.now() + 25 * 86400000).toISOString().slice(0, 10) + 'T10:00';
  await page.getByLabel('Дата и время').fill(future);
  await page.getByRole('button', { name: 'Добавить слот' }).click();
  await expect(page.locator('.schedule-row')).toHaveCount(29);
  await logout(page);
  await page.getByRole('button', { name: 'Открыть онлайн-консультацию' }).click();
  await page.getByRole('textbox', { name: 'Ваш вопрос' }).fill('протекает кран');
  await page.getByRole('button', { name: 'Отправить вопрос' }).click();
  await expect(page.getByRole('button', { name: 'Посмотреть специалиста' })).toBeVisible();
  await page.getByRole('button', { name: 'Посмотреть специалиста' }).click();
  await expect(page.locator('.profile-head')).toContainText('Алексей Морозов');
  expect(errors).toEqual([]);
});

test('mobile catalog, filtering, loaded stock photos and no overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await expect(page.locator('.specialist-card')).toHaveCount(6);
  for (const img of await page.locator('.specialist-card img').all()) {
    await img.scrollIntoViewIfNeeded();
    await expect(img).toHaveJSProperty('complete', true);
    expect(await img.evaluate(i => i.naturalWidth)).toBeGreaterThan(0);
  }
  await page.getByRole('button', { name: 'Обучение', exact: false }).click();
  await expect(page.locator('.specialist-card')).toHaveCount(1);
  await expect(page.locator('.specialist-card')).toContainText('Анна Лебедева');
  await page.getByRole('button', { name: 'Сбросить', exact: true }).click();
  await page.getByRole('textbox', { name: 'Поиск специалистов' }).fill('такого специалиста нет');
  await page.getByRole('button', { name: 'Найти', exact: false }).first().click();
  await expect(page.locator('#cards')).toContainText('Пока никого не нашли');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
