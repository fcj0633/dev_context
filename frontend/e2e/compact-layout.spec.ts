import { expect, test } from '@playwright/test'

for (const [name, width, height] of [
  ['desktop', 1440, 900], ['tablet', 768, 1024], ['mobile', 375, 812], ['narrow', 320, 740],
] as const) test(`compact layout: ${name} leaves space for answers and long titles`, async ({ page }) => {
  await page.setViewportSize({ width, height })
  await page.route('**/api/v1/sessions/compact-title', route => route.fulfill({ json: {
    id: 'compact-title', title: '这是一个很长的会话标题，用于确认标题能够在紧凑布局中正确省略'.repeat(5),
    messages: [], runs: [],
  } }))
  await page.goto('/sessions/compact-title')
  const input = page.getByRole('textbox', { name: '输入问题' })
  await expect(input).toBeVisible()
  await expect.poll(() => input.evaluate(node => node.clientHeight)).toBe(24)
  await expect(page.locator('.page-title')).toContainText('这是一个很长的会话标题')
  expect((await page.locator('.topbar').boundingBox())!.height).toBe(48)
  expect((await page.locator('.composer').boundingBox())!.height).toBe(56)
  expect((await page.locator('.composer-area').boundingBox())!.height).toBe(76)
  for (const control of [page.getByLabel('回答模式'), page.getByRole('button', { name: '发送问题', exact: true })]) {
    expect((await control.boundingBox())!.height).toBeGreaterThanOrEqual(44)
  }
  await expect(page.getByRole('button', { name: '发送问题', exact: true })).toBeDisabled()
  await expect(page.locator('.grounded-badge')).toBeVisible({ visible: width > 480 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  expect(await page.locator('.page-title').evaluate(node => node.scrollWidth > node.clientWidth)).toBe(true)
  const conversation = (await page.locator('.conversation').boundingBox())!
  const composer = (await page.locator('.composer-area').boundingBox())!
  expect(conversation.y + conversation.height).toBeLessThanOrEqual(composer.y)
})

test('composer expands to three lines, scrolls, shrinks and responds to width changes', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto('/')
  const input = page.getByRole('textbox', { name: '输入问题' })
  const inputHeight = () => input.evaluate(node => node.clientHeight)
  await input.fill('第一行\n第二行\n第三行')
  await expect.poll(inputHeight).toBe(72)
  expect((await page.locator('.composer').boundingBox())!.height).toBe(84)
  await input.fill('第一行\n第二行\n第三行\n第四行\n第五行')
  await expect.poll(inputHeight).toBe(72)
  expect(await input.evaluate(node => node.scrollHeight > node.clientHeight && getComputedStyle(node).overflowY === 'auto')).toBe(true)
  await input.fill('短问题')
  await expect.poll(inputHeight).toBe(24)
  await input.fill('项目注册流程需要说明入口位置、接口路径、服务方法与参数校验。')
  await expect.poll(inputHeight).toBe(24)
  await page.setViewportSize({ width: 320, height: 740 })
  await expect.poll(inputHeight).toBe(72)
  await page.setViewportSize({ width: 1440, height: 900 })
  await expect.poll(inputHeight).toBe(24)
  await input.fill('')
  await expect.poll(inputHeight).toBe(24)
})

test('suggestions resize the composer and new chat resets its height', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 740 })
  await page.goto('/')
  const input = page.getByRole('textbox', { name: '输入问题' })
  await page.getByRole('button', { name: /理解实现/ }).click()
  await expect(input).toHaveValue('购票流程如何保证库存与订单的一致性？')
  await expect.poll(() => input.evaluate(node => node.clientHeight)).toBeGreaterThan(24)
  await page.getByRole('button', { name: '打开历史会话' }).click()
  await page.getByRole('button', { name: /新建会话/ }).click()
  await expect(input).toHaveValue('')
  await expect.poll(() => input.evaluate(node => node.clientHeight)).toBe(24)
})

test('compact composer preserves keyboard newlines, mode selection, submission and stop', async ({ page }) => {
  await page.goto('/')
  const input = page.getByRole('textbox', { name: '输入问题' })
  await page.getByLabel('回答模式').selectOption('full')
  await input.fill('第一行')
  await input.press('Shift+Enter')
  await input.press('Shift+Enter')
  await expect.poll(() => input.evaluate(node => node.clientHeight)).toBe(72)
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0)
  await input.fill('slow')
  const sent = page.waitForRequest(request => request.method() === 'POST' && request.url().endsWith('/runs'))
  await input.press('Enter')
  expect((await sent).postDataJSON().profile).toBe('full')
  await expect(input).toHaveValue('')
  await expect.poll(() => input.evaluate(node => node.clientHeight)).toBe(24)
  const stop = page.getByRole('button', { name: '停止生成' })
  await expect(stop).toBeVisible()
  expect((await stop.boundingBox())!.height).toBeGreaterThanOrEqual(44)
  await expect(page.getByLabel('回答模式')).toBeDisabled()
  await stop.click()
  await expect(page.getByText('已停止', { exact: true })).toBeVisible()
})
