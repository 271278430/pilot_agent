/** Product identity through the official WebServer index tap and asset routes. */
import { readFileSync } from 'node:fs'
export const name = 'pilot-brand'
export const inject = ['webServer']
export function projectVersion(text) {
  const section = text.split(/^\[project\]\s*$/m)[1]?.split(/^\[/m)[0]
  const version = section?.match(/^version\s*=\s*["']([^"']+)["']/m)?.[1]
  if (!version || !/^[A-Za-z0-9][A-Za-z0-9.+-]{0,79}$/.test(version)) throw new Error('Invalid Pilot project version')
  return version
}
export function apply(ctx) {
  const version = projectVersion(readFileSync(new URL('../../../../pyproject.toml', import.meta.url), 'utf8'))
  const logo = readFileSync(new URL('./logo.svg', import.meta.url), 'utf8')
  const manifest = JSON.stringify({ name: 'Pilot', short_name: 'Pilot', version, start_url: '/', display: 'standalone',
    background_color: '#f4f5f1', theme_color: '#176b59',
    icons: [{ src: '/pilot-brand.svg', sizes: 'any', type: 'image/svg+xml', purpose: 'any' }] })
  const stylesheet = readFileSync(new URL('./pilot.css', import.meta.url), 'utf8')
  for (const [path, data, type] of [['/pilot-brand.svg', logo, 'image/svg+xml'], ['/pilot.webmanifest', manifest, 'application/manifest+json'], ['/pilot.css', stylesheet, 'text/css']])
    ctx.effect(() => ctx.webServer.register({ kind: 'exact', path,
      resource: async () => new Response(data, { headers: { 'content-type': type, 'cache-control': 'no-cache' } }) }))
  ctx.effect(() => ctx.webServer.tapIndex(html => html
    .replace(/<title>[^<]*<\/title>/i, '<title>Pilot</title>')
    .replace(/<link\b[^>]*\brel=["'](?:icon|apple-touch-icon|manifest)["'][^>]*>/gi, '')
    .replace(/(<meta\b[^>]*name=["']apple-mobile-web-app-title["'][^>]*content=)["'][^"']*["']/i, '$1"Pilot"')
    // The framework-free boot card is constructed before client plugins run.
    // Its stable data marker lets the index style supply the product wordmark
    // from the first frame, including a failed boot, without touching boot logic.
    .replace('</head>', `<meta name="pilot-version" content="${version}"><link rel="icon" type="image/svg+xml" href="/pilot-brand.svg"><link rel="manifest" href="/pilot.webmanifest"><link rel="stylesheet" href="/pilot.css">
      <style data-pilot-boot-brand>
        [data-dsh-boot] > div > div:first-child { display: none !important; }
        [data-dsh-boot] > div::before { content: 'Pilot'; font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif; font-size: 22px; letter-spacing: -.5px; }
      </style></head>`)))
}
