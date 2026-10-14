'use strict';
{
    const $ = django.jQuery;

    function init(select) {
        $(select).select2({width: '400px'});
    }

    $(function() {
        // Skip the hidden template row that Django clones for "Add another"
        $('select.folder-select').not('[name*="__prefix__"]').each(function() {
            init(this);
        });
    });

    document.addEventListener('formset:added', function(event) {
        $(event.target).find('select.folder-select').each(function() {
            init(this);
        });
    });
}
