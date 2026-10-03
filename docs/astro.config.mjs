// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

// Published on GitHub Pages: https://cchabanois.github.io/notosaurus/
export default defineConfig({
	site: 'https://cchabanois.github.io',
	base: '/notosaurus',
	integrations: [
		starlight({
			title: 'Notosaurus',
			description: 'Snap a lesson, get Anki cards.',
			logo: { src: './src/assets/logo-mark.webp' },
			favicon: '/favicon.png',
			customCss: ['./src/styles/notosaurus.css'],
			social: [{ icon: 'github', label: 'GitHub', href: 'https://github.com/cchabanois/notosaurus' }],
			editLink: { baseUrl: 'https://github.com/cchabanois/notosaurus/edit/main/docs/' },
			// English at the root (/notosaurus/), the other languages under their code.
			// A page not translated yet shows the English one, with a notice.
			defaultLocale: 'root',
			locales: {
				root: { label: 'English', lang: 'en' },
				fr: { label: 'Français' },
				es: { label: 'Español' },
				de: { label: 'Deutsch' },
				it: { label: 'Italiano' },
				'pt-br': { label: 'Português (Brasil)', lang: 'pt-BR' },
			},
			sidebar: [
				{
					label: 'Start here',
					translations: {
						fr: 'Pour commencer',
						es: 'Para empezar',
						de: 'Erste Schritte',
						it: 'Per iniziare',
						'pt-BR': 'Para começar',
					},
					items: [{ slug: 'getting-started' }],
				},
				{
					label: 'Guides',
					translations: { fr: 'Guides', es: 'Guías', de: 'Anleitungen', it: 'Guide', 'pt-BR': 'Guias' },
					items: [{ slug: 'several-children' }],
				},
			],
		}),
	],
});
