"""
Database operation mixins and helpers for common patterns.
"""
import sqlite3
from typing import Any, Dict


class DatabaseOperationMixin:
    """
    Mixin class that provides common database operation patterns.
    
    Classes that inherit from this mixin should have a 'db_path' attribute
    that points to the SQLite database file.
    """
    
    def _build_dynamic_update(self, table_name: str, where_column: str, 
                            where_value: Any, include_timestamp: bool = True,
                            **updates) -> None:
        """
        Build and execute a dynamic UPDATE statement with optional fields.
        
        Args:
            table_name: Name of the table to update
            where_column: Column name for the WHERE clause
            where_value: Value for the WHERE clause
            include_timestamp: Whether to include updated_at = CURRENT_TIMESTAMP
            **updates: Field name -> value pairs to update (None values are ignored)
            
        Example:
            self._build_dynamic_update(
                'search_facets', 'id', facet_id,
                status='completed',
                items_discovered=100,
                error_message=None  # This will be ignored
            )
        """
        # Build the dynamic update list
        update_clauses = []
        params = []
        
        # Add timestamp if requested
        if include_timestamp:
            update_clauses.append("updated_at = CURRENT_TIMESTAMP")
        
        # Add all non-None updates
        for field_name, value in updates.items():
            if value is not None:
                if value == 'CURRENT_TIMESTAMP':
                    update_clauses.append(f"{field_name} = CURRENT_TIMESTAMP")
                else:
                    update_clauses.append(f"{field_name} = ?")
                    params.append(value)
        
        # Only proceed if we have something to update
        if not update_clauses:
            return
        
        # Add the WHERE parameter
        params.append(where_value)
        
        # Build and execute the query
        sql = f"""
            UPDATE {table_name} 
            SET {', '.join(update_clauses)}
            WHERE {where_column} = ?
        """
        
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(sql, params)
            conn.commit()
    
    def _build_conditional_update(self, table_name: str, where_column: str,
                                where_value: Any, updates: Dict[str, Any],
                                conditional_updates: Dict[str, Dict[str, Any]] = None) -> None:
        """
        Build and execute a dynamic UPDATE with conditional logic based on status changes.
        
        Args:
            table_name: Name of the table to update
            where_column: Column name for the WHERE clause  
            where_value: Value for the WHERE clause
            updates: Basic field updates {field_name: value}
            conditional_updates: Status-dependent updates {status_value: {field: value}}
            
        Example:
            self._build_conditional_update(
                'download_queue', 'id', queue_id,
                {'status': 'completed', 'progress_percent': 100},
                conditional_updates={
                    'active': {'started_at': 'CURRENT_TIMESTAMP'},
                    'completed': {'completed_at': 'CURRENT_TIMESTAMP'}
                }
            )
        """
        update_clauses = ["updated_at = CURRENT_TIMESTAMP"]
        params = []
        
        # Add basic updates
        for field_name, value in updates.items():
            if value is not None:
                if value == 'CURRENT_TIMESTAMP':
                    update_clauses.append(f"{field_name} = CURRENT_TIMESTAMP")
                else:
                    update_clauses.append(f"{field_name} = ?")
                    params.append(value)
        
        # Add conditional updates based on status
        if conditional_updates and 'status' in updates:
            status_value = updates['status']
            if status_value in conditional_updates:
                for field_name, value in conditional_updates[status_value].items():
                    if value == 'CURRENT_TIMESTAMP':
                        update_clauses.append(f"{field_name} = CURRENT_TIMESTAMP")
                    else:
                        update_clauses.append(f"{field_name} = ?")
                        params.append(value)
        
        # Only proceed if we have something to update beyond timestamp
        if len(update_clauses) <= 1:
            return
        
        # Add the WHERE parameter
        params.append(where_value)
        
        # Build and execute the query
        sql = f"""
            UPDATE {table_name} 
            SET {', '.join(update_clauses)}
            WHERE {where_column} = ?
        """
        
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(sql, params)
            conn.commit()