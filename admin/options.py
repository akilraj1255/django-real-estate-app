import dateutil.parser

from django import forms
from django import template
from django.conf import settings
from django.contrib.admin import ModelAdmin, helpers
from django.contrib.admin.options import FORMFIELD_FOR_DBFIELD_DEFAULTS
from django.contrib.admin.util import unquote, get_deleted_objects, flatten_fieldsets
from django.core.exceptions import PermissionDenied, ObjectDoesNotExist
from django.core import serializers
from django.db import models, transaction, router
from django.forms.formsets import all_valid
from django.forms.models import (modelform_factory)
from django.http import HttpResponse, HttpResponseRedirect, Http404
from django.shortcuts import get_object_or_404, render_to_response
from django.utils.datastructures import SortedDict
from django.utils.decorators import method_decorator
from django.utils.functional import curry, update_wrapper
from django.utils.translation import ugettext as _
from django.utils.encoding import force_unicode
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.utils import simplejson
from django.views.decorators.csrf import csrf_protect

from real_estate_app import widgets
from real_estate_app.admin.actions import delete_selected_popup, make_enabled, make_disabled
from real_estate_app.conf.settings import REAL_ESTATE_APP_AJAX_SEARCH, MEDIA_PREFIX as MEDIA_PREFIX_REAL_ESTATE
from real_estate_app.utils import AutoCompleteObject

from real_estate_app.apps.propertys.models import Property
#TODO: make a wrapp function for delete elements with Property references.

csrf_protect_m = method_decorator(csrf_protect)

FORMFIELD_FOR_DBFIELD_DEFAULTS.update({
    models.DateTimeField: {
        'form_class': forms.SplitDateTimeField,
        'widget': widgets.CustomAdminSplitDateTime
    },
    models.DateField: {'widget':widgets.CustomAdminDateWidget},
})


class FaceBoxModelAdmin(ModelAdmin):

    def _media(self):

        from django.conf import settings
        from real_estate_app.conf.settings import MEDIA_REAL_ESTATE
        js = ['js/core.js', 'js/admin/RelatedObjectLookups.js',
              'js/jquery.min.js', 'js/jquery.init.js']
        jscustom = []
        if self.actions is not None:
            js.extend(['js/actions.min.js'])
        if self.prepopulated_fields:
            js.append('js/urlify.js')
            jscustom.append('%s%s' %(MEDIA_REAL_ESTATE,'admin/js/prepopulatecustom.min.js'))
        if self.opts.get_ordered_objects():
            js.extend(['js/getElementsBySelector.js', 'js/dom-drag.js' , 'js/admin/ordering.js'])
        admin_js=['%s%s' % (settings.ADMIN_MEDIA_PREFIX, url) for url in js]
        admin_js+=jscustom
        return forms.Media(js=admin_js)
    media = property(_media)

    def formfield_for_dbfield(self, db_field, **kwargs):
        """  
        This is for create a widget FaceBoxFieldWrapper to add new itens on ModelAdmin.
        """
        request = kwargs.pop("request", None)

        # If the field specifies choices, we don't need to look for special
        # admin widgets - we just need to use a select widget of some kind.
        if db_field.choices:
            return self.formfield_for_choice_field(db_field, request, **kwargs)

        # ForeignKey or ManyToManyFields
        if isinstance(db_field, (models.ForeignKey, models.ManyToManyField)):
            # Combine the field kwargs with any options for formfield_overrides.
            # Make sure the passed in **kwargs override anything in
            # formfield_overrides because **kwargs is more specific, and should
            # always win.
            if db_field.__class__ in self.formfield_overrides:
                kwargs = dict(self.formfield_overrides[db_field.__class__], **kwargs)

            # Get the correct formfield.
            if isinstance(db_field, models.ForeignKey):
                formfield = self.formfield_for_foreignkey(db_field, request, **kwargs)
            elif isinstance(db_field, models.ManyToManyField):
                formfield = self.formfield_for_manytomany(db_field, request, **kwargs)

            # For non-raw_id fields, wrap the widget with a wrapper that adds
            # extra HTML -- the "add other" interface -- to the end of the
            # rendered output. formfield can be None if it came from a
            # OneToOneField with parent_link=True or a M2M intermediary.
            if formfield and db_field.name not in self.raw_id_fields:
                related_modeladmin = self.admin_site._registry.get(
                                                            db_field.rel.to)
                can_add_related = bool(related_modeladmin and
                            related_modeladmin.has_add_permission(request))
                formfield.widget = widgets.FaceBoxFieldWrapper(formfield.widget,
                                                                db_field.rel, 
                                                                self.admin_site,
                                                                can_add_related=can_add_related)
            return formfield

        # If we've got overrides for the formfield defined, use 'em. **kwargs
        # passed to formfield_for_dbfield override the defaults.
        for klass in db_field.__class__.mro():
            if klass in self.formfield_overrides:
                kwargs = dict(self.formfield_overrides[klass], **kwargs)
                return db_field.formfield(**kwargs)

        # For any other type of field, just call its formfield() method.
        return db_field.formfield(**kwargs)

    @csrf_protect_m
    @transaction.commit_on_success
    def add_view(self, request, form_url='', extra_context=None):
        "The 'add' admin view for this model. \
         This function has been customized for datetime initial like a ticket #19431"
        model = self.model
        opts = model._meta

        if not self.has_add_permission(request):
            raise PermissionDenied

        ModelForm = self.get_form(request)
        formsets = []
        if request.method == 'POST':
            form = ModelForm(request.POST, request.FILES)
            if form.is_valid():
                new_object = self.save_form(request, form, change=False)
                form_validated = True
            else:
                form_validated = False
                new_object = self.model()
            prefixes = {}
            for FormSet, inline in zip(self.get_formsets(request), self.inline_instances):
                prefix = FormSet.get_default_prefix()
                prefixes[prefix] = prefixes.get(prefix, 0) + 1
                if prefixes[prefix] != 1:
                    prefix = "%s-%s" % (prefix, prefixes[prefix])
                formset = FormSet(data=request.POST, files=request.FILES,
                                  instance=new_object,
                                  save_as_new="_saveasnew" in request.POST,
                                  prefix=prefix, queryset=inline.queryset(request))
                formsets.append(formset)
            if all_valid(formsets) and form_validated:
                self.save_model(request, new_object, form, change=False)
                form.save_m2m()
                for formset in formsets:
                    self.save_formset(request, form, formset, change=False)

                self.log_addition(request, new_object)
                return self.response_add(request, new_object)
        else:
            # Prepare the dict of initial data from the request.
            # We have to special-case M2Ms as a list of comma-separated PKs.
            initial = dict(request.GET.items())
            for k in initial:
                try:
                    f = opts.get_field(k)
                except models.FieldDoesNotExist:
                    continue
                if isinstance(f, models.ManyToManyField):
                    initial[k] = initial[k].split(",")
                elif isinstance(f, models.DateTimeField):
                    initial[k] = dateutil.parser.parse(initial[k])
            form = ModelForm(initial=initial)
            prefixes = {}
            for FormSet, inline in zip(self.get_formsets(request),
                                       self.inline_instances):
                prefix = FormSet.get_default_prefix()
                prefixes[prefix] = prefixes.get(prefix, 0) + 1
                if prefixes[prefix] != 1:
                    prefix = "%s-%s" % (prefix, prefixes[prefix])
                formset = FormSet(instance=self.model(), prefix=prefix,
                                  queryset=inline.queryset(request))
                formsets.append(formset)

        adminForm = helpers.AdminForm(form, list(self.get_fieldsets(request)),
            self.prepopulated_fields, self.get_readonly_fields(request),
            model_admin=self)
        media = self.media + adminForm.media

        inline_admin_formsets = []
        for inline, formset in zip(self.inline_instances, formsets):
            fieldsets = list(inline.get_fieldsets(request))
            readonly = list(inline.get_readonly_fields(request))
            inline_admin_formset = helpers.InlineAdminFormSet(inline, formset,
                fieldsets, readonly, model_admin=self)
            inline_admin_formsets.append(inline_admin_formset)
            media = media + inline_admin_formset.media

        context = {
            'title': _('Add %s') % force_unicode(opts.verbose_name),
            'adminform': adminForm,
            'is_popup': "_popup" in request.REQUEST,
            'show_delete': False,
            'media': mark_safe(media),
            'inline_admin_formsets': inline_admin_formsets,
            'errors': helpers.AdminErrorList(form, formsets),
            'root_path': self.admin_site.root_path,
            'app_label': opts.app_label,
        }
        context.update(extra_context or {})
        return self.render_change_form(request, context, form_url=form_url, add=True)

    def get_urls(self):

        from django.conf.urls.defaults import patterns, url

        urlpatterns = super(FaceBoxModelAdmin,self).get_urls()

        def wrap(view):
            def wrapper(*args, **kwargs):
                return self.admin_site.admin_view(view)(*args, **kwargs)
            return update_wrapper(wrapper, view)

        info = self.model._meta.app_label, self.model._meta.module_name

        custom_urls = patterns('',
                                url(r'^ajax/$',
                                    wrap(self.ajax_view),
                                    name='%s_%s_ajax_view' % info
                                ),
        )

        return custom_urls + urlpatterns

    @csrf_protect_m 
    def ajax_view(self, request, extra_context=None):
        """
        Ajax interation to construct the select options
        used a custom serialize how get some expecific fields.
        """
        q_value=''
        opts=self.model._meta

        model = self.model
        model_name=model.__name__.lower()
        if hasattr(model.objects, 'all_enabled'):
            queryset = model.objects.all_enabled()
        else:
            queryset = model.objects.all()
        module_name=opts.module_name
        
        #fields = REAL_ESTATE_APP_AJAX_SEARCH.get(model_name,False).get('label',False)
        fields = [i.name for i in model._meta.fields]

        if request.POST:
            if request.POST.items():
                
                for query in request.POST.items():
                    if 'csrfmiddlewaretoken' not in query:
                        query=dict((query,))
                        queryset=queryset.filter(**query)
        else:
            # This is for ajax
            if 'term' in request.GET:
                q_value=request.GET['term']
                return HttpResponse(
                                    simplejson.dumps(
                                        AutoCompleteObject(model).render(value=q_value,logical_exclude__exact=False)
                                    )
                                    ,mimetype="text/javascript")

        json = serializers.serialize("json", queryset,fields=fields)
        return HttpResponse(json, mimetype="text/javascript")

    class Media:

        css = {
            'all':(
                    MEDIA_PREFIX_REAL_ESTATE+"admin/css/facebox.css",
            ),
        }

        js = [
            settings.ADMIN_MEDIA_PREFIX+"js/jquery.min.js",
            settings.ADMIN_MEDIA_PREFIX+"js/jquery.init.js",
            MEDIA_PREFIX_REAL_ESTATE+"admin/js/facebox.js",
        ]

class RealEstateAppPopUpModelAdmin(FaceBoxModelAdmin):
    """
        All fields with have a ForeignKey must be named with '<var_name>_fk'
        because this '_fk' is used to identify what field has to be loaded on
        facebox.
    """

    list_per_page=15

    actions=[delete_selected_popup, make_enabled, make_disabled]

    list_filter=['logical_exclude',]

    exclude = ('create_date',)

    def __init__(self, *args,**kwargs):
        super(RealEstateAppPopUpModelAdmin,self).__init__(*args,**kwargs)
        overrides = FORMFIELD_FOR_DBFIELD_DEFAULTS.copy()
        overrides.update(self.formfield_overrides)
        self.formfield_overrides = overrides


    def delete_model(self, request, obj):
        obj_fk = obj._meta.module_name+'_fk'
        obj_name=obj._meta.module_name

        if hasattr(Property,obj_fk) and not isinstance(obj,Property):

            try:
                Property.objects.get(**{obj_fk:obj.id})
                obj.logical_exclude=True
                obj.save()
                return _('The %(name)s "%(obj)s" was disabled successfully.')
            except ObjectDoesNotExist:
                pass

        super(RealEstateAppPopUpModelAdmin,self).delete_model(request,obj)
        return _('The %(name)s "%(obj)s" was deleted successfully.')

    def get_actions(self,request):
        """
        This is a original get_actions with removed validation of IS_POPUP_VAR 
        """
        # If self.actions is explicitally set to None that means that we don't
        # want *any* actions enabled on this page.
        # REMOVE VALIDATION OF IS_POPUP_VAR
        if self.actions is None:
            return SortedDict()

        actions = []

        # Gather actions from the admin site first
        for (name, func) in self.admin_site.actions:
            description = getattr(func, 'short_description', name.replace('_', ' '))
            actions.append((func, name, description))

        # Then gather them from the model admin and all parent classes,
        # starting with self and working back up.
        for klass in self.__class__.mro()[::-1]:
            class_actions = getattr(klass, 'actions', [])
            # Avoid trying to iterate over None
            if not class_actions:
                continue
            actions.extend([self.get_action(action) for action in class_actions])

        # get_action might have returned None, so filter any of those out.
        actions = filter(None, actions)

        # Convert the actions into a SortedDict keyed by name
        # and sorted by description.
        actions.sort(key=lambda k: k[2].lower())
        actions = SortedDict([
            (name, (func, name, desc))
            for func, name, desc in actions
        ])

        if 'delete_selected' in actions:
            del actions['delete_selected']
        return actions

    def get_form(self, request, obj=None, **kwargs):
        """
        Returns a Form class for use in the admin add view. This is used by
        add_view and change_view.
        """
        from django.db.models.loading import get_model

        form = super(RealEstateAppPopUpModelAdmin,self).get_form(request,obj,**kwargs)
        
        for key, field in form.base_fields.items():
            if isinstance(field.widget,(widgets.AdminAjaxSelectMultipleInputWidget,widgets.CheckboxSelectMultipleCustom)):
                if hasattr(field.widget,'model'):
                    model = field.widget.model
                elif hasattr(field.widget,'module_name') and hasattr(field.widget,'app_label'):
                    model = get_model(app_label=app_label,module_name=module_name)

                related_modeladmin = self.admin_site._registry.get(model)
                can_add_related = bool(related_modeladmin and
                            related_modeladmin.has_add_permission(request))
                
                field.widget.can_add_related=can_add_related
        return form


    def response_add(self, request, obj, post_url_continue='../%s/'):
        opts = obj._meta
        pk_value = obj._get_pk_val()
        post_url='../'
        
        msg = _('The %(name)s "%(obj)s" was added successfully.') % {'name': force_unicode(opts.verbose_name), 'obj': force_unicode(obj)}
        # Here, we distinguish between different save types by checking for
        # the presence of keys in request.POST.
        if "_continue" in request.POST:
            self.message_user(request, msg + ' ' + _("You may edit it again below."))
            if "_popup" in request.POST:
                post_url_continue += "?_popup=1"
            return HttpResponseRedirect(post_url_continue % pk_value)

        if "_popup" in request.POST:
            post_url += "?pop=1"
            return HttpResponseRedirect(post_url)
        elif "_addanother" in request.POST:
            self.message_user(request, msg + ' ' + (_("You may add another %s below.") % force_unicode(opts.verbose_name)))
            return HttpResponseRedirect(request.path)
        else:
            self.message_user(request, msg)

            # Figure out where to redirect. If the user has change permission,
            # redirect to the change-list page for this object. Otherwise,
            # redirect to the admin index.

            if self.has_change_permission(request, None):
                post_url = post_url
            else:
                post_url = '../../../'
            return HttpResponseRedirect(post_url)

    def response_change(self,request,obj):
        """
        Determines the HttpResponse for the change_view stage.
        """
        opts = obj._meta

        # Handle proxy models automatically created by .only() or .defer()
        verbose_name = opts.verbose_name
        if obj._deferred:
            opts_ = opts.proxy_for_model._meta
            verbose_name = opts_.verbose_name

        pk_value = obj._get_pk_val()

        msg = _('The %(name)s "%(obj)s" was changed successfully.') % {'name': force_unicode(verbose_name), 'obj': force_unicode(obj)}
        if "_continue" in request.POST:
            self.message_user(request, msg + ' ' + _("You may edit it again below."))
            if "_popup" in request.REQUEST:
                return HttpResponseRedirect(request.path + "?_popup=1")
            else:
                return HttpResponseRedirect(request.path)
                
        if "_popup" in request.POST:
            return HttpResponseRedirect('../?pop=1')
        elif "_saveasnew" in request.POST:
            msg = _('The %(name)s "%(obj)s" was added successfully. You may edit it again below.') % {'name': force_unicode(verbose_name), 'obj': obj}
            self.message_user(request, msg)
            return HttpResponseRedirect("../%s/" % pk_value)
        elif "_addanother" in request.POST:
            self.message_user(request, msg + ' ' + (_("You may add another %s below.") % force_unicode(verbose_name)))
            return HttpResponseRedirect("../add/")
        else:
            self.message_user(request, msg)
            # Figure out where to redirect. If the user has change permission,
            # redirect to the change-list page for this object. Otherwise,
            # redirect to the admin index.
            if self.has_change_permission(request, None):
                return HttpResponseRedirect('../')
            else:
                return HttpResponseRedirect('../../../')
        

    def get_urls(self):

        from django.conf.urls.defaults import patterns, url

        urlpatterns = super(RealEstateAppPopUpModelAdmin,self).get_urls()

        def wrap(view):
            def wrapper(*args, **kwargs):
                return self.admin_site.admin_view(view)(*args, **kwargs)
            return update_wrapper(wrapper, view)

        info = self.model._meta.app_label, self.model._meta.module_name

        custom_urls = patterns('',
                                url(r'^add/$',
                                    wrap(self.add_view_popup),
                                    name='%s_%s_add_popup' % info
                                ),
                                url(r'^(?P<object_id>\d+)/$',
                                    wrap(self.change_view_popup),
                                    name='%s_%s_chage_popup' % info 
                                ),
                                url(r'^/$',
                                    wrap(self.changelist_view_popup),
                                    name='%s_%s_view_popup' % info
                                ),
                                url(r'^(?P<object_id>\d+)/delete/$',
                                    wrap(self.delete_view_popup),
                                    name='%s_%s_delete_popup' % info
                                ),
        )

        return custom_urls + urlpatterns

    @csrf_protect_m
    def changelist_view(self, request, extra_context={}):
        extra_context.update({'has_change_permission':self.has_change_permission(request)})
        return super(RealEstateAppPopUpModelAdmin,self).changelist_view(request, extra_context=extra_context)

    @csrf_protect_m
    @transaction.commit_on_success
    def add_view_popup(self, request, extra_context=None):
        return super(RealEstateAppPopUpModelAdmin,self).add_view(request, extra_context=extra_context)

    @csrf_protect_m
    @transaction.commit_on_success
    def change_view_popup(self, request, object_id=None, extra_context={}):
        extra_context=extra_context.update({'has_change_permission':self.has_change_permission(request)})
        return super(RealEstateAppPopUpModelAdmin,self).change_view(request, object_id, extra_context=extra_context)

    @csrf_protect_m
    def changelist_view_popup(self, request, extra_context={}):
        if not request.GET.has_key('logical_exclude__exact'):
            get=request.GET.copy()
            get['logical_exclude__exact']='0'
            request.GET = get
            request.META['QUERY_STRING']=request.GET.urlencode()
            extra_context=extra_context.update({'has_change_permission':self.has_change_permission(request)})
        return super(RealEstateAppPopUpModelAdmin,self).changelist_view(request, extra_context=extra_context)

    @csrf_protect_m
    @transaction.commit_on_success
    def delete_view_popup(self, request, object_id, extra_context=None):  
        "The 'delete' admin view for reverted model."
        opts = self.model._meta
        app_label = opts.app_label
        notabs = {'notabs':True}
        try:
            extra_context = extra_context.update(notabs)
        except:
            extra_context = notabs

        obj = self.get_object(request, unquote(object_id))

        if not self.has_delete_permission(request, obj):
            raise PermissionDenied

        if obj is None:
            raise Http404(_('%(name)s object with primary key %(key)r does not exist.') % {'name': force_unicode(opts.verbose_name), 'key': escape(object_id)})

        using = router.db_for_write(self.model)

        # Populate deleted_objects, a data structure of all related objects that
        # will also be deleted.
        (deleted_objects, perms_needed, protected) = get_deleted_objects(
            [obj], opts, request.user, self.admin_site, using)

        if request.POST: # The user has already confirmed the deletion.
            if perms_needed:
                raise PermissionDenied
            obj_display = force_unicode(obj)
            self.log_deletion(request, obj, obj_display)
            msg=self.delete_model(request, obj)

            self.message_user(request, msg % {'name': force_unicode(opts.verbose_name), 'obj': force_unicode(obj_display)})

            if not self.has_change_permission(request, None):
                return HttpResponseRedirect("../../../../")
            return HttpResponseRedirect("../../")

        object_name = force_unicode(opts.verbose_name)

        if perms_needed or protected:
            title = _("Cannot delete %(name)s") % {"name": object_name}
        else:
            title = _("Are you sure?")

        context = {
            "title": title,
            "object_name": object_name,
            "object": obj,
            "deleted_objects": deleted_objects,
            "perms_lacking": perms_needed,
            "protected": protected,
            "opts": opts,
            "root_path": self.admin_site.root_path,
            "app_label": app_label,
        }
        context.update(extra_context or {})
        context_instance = template.RequestContext(request, current_app=self.admin_site.name)
        return render_to_response(self.delete_confirmation_template or [
            "admin/%s/%s/delete_confirmation.html" % (app_label, opts.object_name.lower()),
            "admin/%s/delete_confirmation.html" % app_label,
            "admin/delete_confirmation.html"
        ], context, context_instance=context_instance)

    class Media:

        js = [
            settings.ADMIN_MEDIA_PREFIX+"js/jquery.min.js",
            settings.ADMIN_MEDIA_PREFIX+"js/jquery.init.js",
        ]

class RealEstateAppRevertInlineModelAdmin(RealEstateAppPopUpModelAdmin):

    def __init__(self, model, admin_site):
        """
            This is a custom init for create a revert inlines formsets.
        """
        self.model = model
        self.opts = model._meta
        self.admin_site = admin_site
        self.inline_instances = []
        for inline_class in self.revert_inlines:
            inline_instance = inline_class(self.revert_model, self.admin_site)
            self.inline_instances.append(inline_instance)
        if 'action_checkbox' not in self.list_display and self.actions is not None:
            self.list_display = ['action_checkbox'] +  list(self.list_display)
        if not self.list_display_links:
            for name in self.list_display:
                if name != 'action_checkbox':
                    self.list_display_links = [name]
                    break

        #super(ModelAdmin,self).__init__()
    def delete_model(self, request, revert_obj,real_obj):
        obj_fk = real_obj._meta.module_name+'_fk'
        obj_name=real_obj._meta.module_name

        if hasattr(Property,obj_fk) and not isinstance(revert_obj,Property):
                try:
                    Property.objects.get(**{obj_fk:real_obj.id})
                    real_obj.logical_exclude=True
                    real_obj.save()
                    return _('The %(name)s "%(obj)s" was disabled successfully.')
                except ObjectDoesNotExist:
                   pass

        super(RealEstateAppRevertInlineModelAdmin,self).delete_model(request,revert_obj)
        return _('The %(name)s "%(obj)s" was deleted successfully.')

    def get_form(self, request, obj=None, **kwargs):
        """
        Custom get_form function to returns a revert form class for use in the admin add view. 
        This is used by add_view and change_view.
        """
        from django.db.models.loading import get_model

        if self.declared_fieldsets:
            fields = flatten_fieldsets(self.declared_fieldsets)
        else:
            fields = None
        if self.exclude is None:
            exclude = []
        else:
            exclude = list(self.exclude)
        exclude.extend(kwargs.get("exclude", []))
        exclude.extend(self.get_readonly_fields(request, obj))
        # if exclude is an empty list we pass None to be consistant with the
        # default on modelform_factory
        exclude = exclude or None
        defaults = {
            "form": self.revert_form,
            "fields": fields,
            "exclude": exclude,
            "formfield_callback": curry(self.formfield_for_dbfield, request=request),
        }
        defaults.update(kwargs)
        form = modelform_factory(self.revert_model, **defaults)
        
        for key, field in form.base_fields.items():
            if isinstance(field.widget,(widgets.AdminAjaxSelectMultipleInputWidget,widgets.CheckboxSelectMultipleCustom)):
                if hasattr(field.widget,'model'):
                    model = field.widget.model
                elif hasattr(field.widget,'module_name') and hasattr(field.widget,'app_label'):
                    model = get_model(app_label=app_label,module_name=module_name)

                related_modeladmin = self.admin_site._registry.get(model)
                can_add_related = bool(related_modeladmin and
                            related_modeladmin.has_add_permission(request))
                
                field.widget.can_add_related=can_add_related

        return form

    @csrf_protect_m
    @transaction.commit_on_success
    def add_view_popup(self, request, form_url='', extra_context=None):
        "The 'add' admin view for this model."
        model = self.model
        opts = model._meta

        notabs = {'notabs':True}
        try:
            extra_context = extra_context.update(notabs)
        except:
            extra_context = notabs

        if not self.has_add_permission(request):
            raise PermissionDenied

        ModelForm = self.get_form(request)
        formsets = []
        if request.method == 'POST':
            
            form = ModelForm(request.POST, request.FILES)
            if form.is_valid():
                new_object = self.save_form(request, form, change=False)
                form_validated = True
            else:
                form_validated = False
                new_object = self.revert_model()

            prefixes = {}

            for FormSet, inline in zip(self.get_formsets(request), self.inline_instances):
                prefix = FormSet.get_default_prefix()
                prefixes[prefix] = prefixes.get(prefix, 0) + 1
                if prefixes[prefix] != 1:
                    prefix = "%s-%s" % (prefix, prefixes[prefix])
                
                formset = FormSet(data=request.POST, files=request.FILES,
                                  instance=new_object,
                                  save_as_new="_saveasnew" in request.POST,
                                  prefix=prefix, queryset=inline.queryset(request))
                
                formsets.append(formset)
            if all_valid(formsets) and form_validated:
                self.save_model(request, new_object, form, change=False)
                form.save_m2m()
                for formset in formsets:
                    self.save_formset(request, form, formset, change=False)

                self.log_addition(request, new_object)
                return self.response_add(request, new_object)
        else:

            # Prepare the dict of initial data from the request.
            # We have to special-case M2Ms as a list of comma-separated PKs.
            initial = dict(request.GET.items())
            for k in initial:
                try:
                    f = opts.get_field(k)
                except models.FieldDoesNotExist:
                    continue
                if isinstance(f, models.ManyToManyField):
                    initial[k] = initial[k].split(",")
            form = ModelForm(initial=initial)
            prefixes = {}
            for FormSet, inline in zip(self.get_formsets(request),
                                       self.inline_instances):
                prefix = FormSet.get_default_prefix()
                prefixes[prefix] = prefixes.get(prefix, 0) + 1
                if prefixes[prefix] != 1:
                    prefix = "%s-%s" % (prefix, prefixes[prefix])
                formset = FormSet(instance=self.revert_model(), prefix=prefix,
                                  queryset=inline.queryset(request))
                formsets.append(formset)

        adminForm = helpers.AdminForm(form, list(self.get_fieldsets(request)),
            self.prepopulated_fields, self.get_readonly_fields(request),
            model_admin=self)
        media = self.media + adminForm.media

        inline_admin_formsets = []
        for inline, formset in zip(self.inline_instances, formsets):
            fieldsets = list(inline.get_fieldsets(request))
            readonly = list(inline.get_readonly_fields(request))
            inline_admin_formset = helpers.InlineAdminFormSet(inline, formset,
                fieldsets, readonly, model_admin=self)
            inline_admin_formsets.append(inline_admin_formset)
            media = media + inline_admin_formset.media

        context = {
            'title': _('Add %s') % force_unicode(opts.verbose_name),
            'adminform': adminForm,
            'is_popup': "_popup" in request.REQUEST,
            'show_delete': False,
            'media': mark_safe(media),
            'inline_admin_formsets': inline_admin_formsets,
            'errors': helpers.AdminErrorList(form, formsets),
            'root_path': self.admin_site.root_path,
            'app_label': opts.app_label,
        }
        context.update(extra_context or {})
        return self.render_change_form(request, context, form_url=form_url, add=True)

    @csrf_protect_m
    @transaction.commit_on_success
    def change_view_popup(self, request, object_id, extra_context=None):
        "The 'change' admin view for reverted model."
        model = self.model
        opts = model._meta
        notabs = {'notabs':True}
        try:
            extra_context = extra_context.update(notabs)
        except:
            extra_context = notabs

        obj = self.get_object(request, unquote(object_id))
        
        revert_model_name = self.revert_model.__name__.lower()
        if hasattr(obj,revert_model_name):
            revert_obj = getattr(obj,revert_model_name)
            obj = revert_obj
        
        
        if not self.has_change_permission(request, obj):
            raise PermissionDenied

        if obj is None:
            raise Http404(_('%(name)s object with primary key %(key)r does not exist.') % {'name': force_unicode(opts.verbose_name), 'key': escape(object_id)})

        if request.method == 'POST' and "_saveasnew" in request.POST:
            return self.add_view(request, form_url='../add/')

        ModelForm = self.get_form(request, obj)
        formsets = []
        if request.method == 'POST':
            form = ModelForm(request.POST, request.FILES, instance=obj)
            if form.is_valid():
                form_validated = True
                new_object = self.save_form(request, form, change=True)
            else:
                form_validated = False
                new_object = obj
            prefixes = {}
            for FormSet, inline in zip(self.get_formsets(request, new_object),
                                       self.inline_instances):
                prefix = FormSet.get_default_prefix()
                prefixes[prefix] = prefixes.get(prefix, 0) + 1
                if prefixes[prefix] != 1:
                    prefix = "%s-%s" % (prefix, prefixes[prefix])
                formset = FormSet(request.POST, request.FILES,
                                  instance=new_object, prefix=prefix,
                                  queryset=inline.queryset(request))

                formsets.append(formset)

            if all_valid(formsets) and form_validated:
                self.save_model(request, new_object, form, change=True)
                form.save_m2m()
                for formset in formsets:
                    self.save_formset(request, form, formset, change=True)

                change_message = self.construct_change_message(request, form, formsets)
                self.log_change(request, new_object, change_message)
                return self.response_change(request, new_object)

        else:
            form = ModelForm(instance=obj)
            prefixes = {}
            for FormSet, inline in zip(self.get_formsets(request, obj), self.inline_instances):
                prefix = FormSet.get_default_prefix()
                prefixes[prefix] = prefixes.get(prefix, 0) + 1
                if prefixes[prefix] != 1:
                    prefix = "%s-%s" % (prefix, prefixes[prefix])
                formset = FormSet(instance=obj, prefix=prefix,
                                  queryset=inline.queryset(request))
                formsets.append(formset)

        adminForm = helpers.AdminForm(form, self.get_fieldsets(request, obj),
            self.prepopulated_fields, self.get_readonly_fields(request, obj),
            model_admin=self)
        media = self.media + adminForm.media

        inline_admin_formsets = []
        for inline, formset in zip(self.inline_instances, formsets):
            fieldsets = list(inline.get_fieldsets(request, obj))
            readonly = list(inline.get_readonly_fields(request, obj))
            inline_admin_formset = helpers.InlineAdminFormSet(inline, formset,
                fieldsets, readonly, model_admin=self)
            inline_admin_formsets.append(inline_admin_formset)
            media = media + inline_admin_formset.media
        
        context = {
            'title': _('Change %s') % force_unicode(opts.verbose_name),
            'adminform': adminForm,
            'object_id': object_id,
            'original': obj,
            'is_popup': "_popup" in request.REQUEST,
            'media': mark_safe(media),
            'inline_admin_formsets': inline_admin_formsets,
            'errors': helpers.AdminErrorList(form, formsets),
            'root_path': self.admin_site.root_path,
            'app_label': opts.app_label,
            'has_change_permission':self.has_change_permission(request)
        }
        context.update(extra_context or {})
        return self.render_change_form(request, context, change=True, obj=obj)

    @csrf_protect_m
    @transaction.commit_on_success
    def delete_view_popup(self, request, object_id, extra_context=None):    
        "The 'delete' admin view for reverted model."
        opts = self.model._meta
        app_label = opts.app_label
        notabs = {'notabs':True}
        try:
            extra_context = extra_context.update(notabs)
        except:
            extra_context = notabs

        obj = self.get_object(request, unquote(object_id))

        revert_model_name = self.revert_model.__name__.lower()
        if hasattr(obj,revert_model_name):
            revert_obj = getattr(obj,revert_model_name)
            real_obj = obj
            obj = revert_obj

        if not self.has_delete_permission(request, obj):
            raise PermissionDenied

        if obj is None:
            raise Http404(_('%(name)s object with primary key %(key)r does not exist.') % {'name': force_unicode(opts.verbose_name), 'key': escape(object_id)})

        using = router.db_for_write(self.model)

        # Populate deleted_objects, a data structure of all related objects that
        # will also be deleted.
        (deleted_objects, perms_needed, protected) = get_deleted_objects(
            [obj], opts, request.user, self.admin_site, using)

        if request.POST: # The user has already confirmed the deletion.
            if perms_needed:
                raise PermissionDenied
            obj_display = force_unicode(obj)
            self.log_deletion(request, obj, obj_display)
            msg=self.delete_model(request, obj,real_obj)

            self.message_user(request, msg % {'name': force_unicode(opts.verbose_name), 'obj': force_unicode(obj_display)})

            if not self.has_change_permission(request, None):
                return HttpResponseRedirect("../../../../")
            return HttpResponseRedirect("../../")

        object_name = force_unicode(opts.verbose_name)

        if perms_needed or protected:
            title = _("Cannot delete %(name)s") % {"name": object_name}
        else:
            title = _("Are you sure?")

        context = {
            "title": title,
            "object_name": object_name,
            "object": obj,
            "deleted_objects": deleted_objects,
            "perms_lacking": perms_needed,
            "protected": protected,
            "opts": opts,
            "root_path": self.admin_site.root_path,
            "app_label": app_label,
        }
        context.update(extra_context or {})
        context_instance = template.RequestContext(request, current_app=self.admin_site.name)
        return render_to_response(self.delete_confirmation_template or [
            "admin/%s/%s/delete_confirmation.html" % (app_label, opts.object_name.lower()),
            "admin/%s/delete_confirmation.html" % app_label,
            "admin/delete_confirmation.html"
        ], context, context_instance=context_instance)