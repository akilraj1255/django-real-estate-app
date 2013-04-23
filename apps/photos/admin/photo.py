from django.utils.translation import ugettext_lazy as _
from django.contrib import admin 
from real_estate_app.apps.photos.models import Photo
from real_estate_app.apps.photos.admin.forms import PhotoAdminForm

class PhotoInlineAdmin(admin.StackedInline):

	prepopulated_fields = {"slug": ("photo",)}
	model = Photo
	extra = 1
	form = PhotoAdminForm 
	template = 'admin/real_estate_app/edit_inline/stacked.html'