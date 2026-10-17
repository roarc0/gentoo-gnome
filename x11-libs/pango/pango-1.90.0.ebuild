# Copyright 1999-2026 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

EAPI=8

inherit flag-o-matic gnome2-utils meson-multilib xdg

DESCRIPTION="Internationalized text layout and rendering library"
HOMEPAGE="https://www.gtk.org/docs/architecture/pango https://gitlab.gnome.org/GNOME/pango"
SRC_URI="https://download.gnome.org/sources/pango/$(ver_cut 1-2)/${P}.tar.xz"

LICENSE="LGPL-2+"
SLOT="0"
KEYWORDS="~alpha ~amd64 ~arm ~arm64 ~hppa ~loong ~m68k ~mips ~ppc ~ppc64 ~riscv ~s390 ~sparc ~x86"

IUSE="debug libthai gtk-doc +introspection sysprof"
REQUIRED_USE="gtk-doc? ( introspection )"

RDEPEND="
	>=dev-libs/glib-2.88:2[${MULTILIB_USEDEP}]
	>=dev-libs/fribidi-1.0.6[${MULTILIB_USEDEP}]
	>=media-libs/harfbuzz-11.0.0:=[glib(+),introspection?,truetype(+),${MULTILIB_USEDEP}]
	>=media-libs/fontconfig-2.17.0:1.0[${MULTILIB_USEDEP}]
	>=x11-libs/cairo-1.18.0[${MULTILIB_USEDEP}]
	>=media-libs/freetype-2.5.0.1:2[harfbuzz,png,${MULTILIB_USEDEP}]
	introspection? ( >=dev-libs/gobject-introspection-1.83.2:= )
	libthai? ( dev-libs/libthai:= )
	sysprof? ( >=dev-util/sysprof-capture-3.40.1:4[${MULTILIB_USEDEP}] )
"
DEPEND="${RDEPEND}"
BDEPEND="
	dev-util/glib-utils
	virtual/pkgconfig
	gtk-doc? (
		introspection? (
			>=dev-libs/gobject-introspection-1.83.2
			dev-util/gi-docgen
		)
	)
"

src_prepare() {
	default
	xdg_environment_reset
	gnome2_environment_reset

	# get rid of a win32 example
	rm examples/pangowin32tobmp.c || die

	# Skip broken test: https://gitlab.gnome.org/GNOME/pango/-/issues/677
	rm tests/layouts/valid-20.layout || die

}

multilib_src_configure() {
	if use debug; then
		append-cflags -DPANGO_ENABLE_DEBUG
	else
		append-cflags -DG_DISABLE_CAST_CHECKS
	fi
	# pangocairo-font.c uses FreeType directly but upstream meson.build
	# doesn't declare it as an explicit dep for the pangocairo component
	append-ldflags -lfreetype

	local emesonargs=(
		# Never use gi-docgen subproject
		--wrap-mode nofallback

		$(meson_native_use_bool gtk-doc documentation)
		$(meson_native_use_feature introspection)
		$(meson_feature sysprof)
		$(meson_native_use_feature libthai)
		-Dcairo=enabled
	)
	meson_src_configure
}

multilib_src_install_all() {
	if use gtk-doc; then
		mkdir -p "${ED}"/usr/share/gtk-doc/html/ || die
		mv "${ED}"/usr/share/doc/Pango* "${ED}"/usr/share/gtk-doc/html/ || die
	fi
	# ponytail: compat shims so old .pc files (gtk4.pc, librsvg-2.0.pc, …)
	# resolve until they're rebuilt against pango2; pangoft2 was dropped as a
	# separate sub-library in 2.0 but the compat .so still ships
	dosym pango2.pc /usr/lib64/pkgconfig/pango.pc
	dosym pangocairo2.pc /usr/lib64/pkgconfig/pangocairo.pc
	cat > "${ED}"/usr/lib64/pkgconfig/pangoft2.pc <<-EOF
		prefix=/usr
		libdir=\${prefix}/lib64
		Name: Pango FreeType
		Description: FreeType font support for Pango (compat stub for pango-2.0)
		Version: 1.90.0
		Requires: pango2
		Libs: -L\${libdir} -lpangoft2-1.0
		Cflags:
	EOF
}
