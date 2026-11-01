package com.example.menu2;

import android.os.Bundle;

import androidx.activity.EdgeToEdge;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;
import android.view.View;
import android.view.Menu;
import android.view.ContextMenu;
import android.view.MenuItem;
import android.widget.*;
public class MainActivity extends AppCompatActivity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        EdgeToEdge.enable(this);
        setContentView(R.layout.activity_main);
        TextView tv = findViewById(R.id.tv);
        registerForContextMenu(tv);
    }
    public void onCreateContextMenu(ContextMenu menu,View v, ContextMenu.ContextMenuInfo menuInfo){
        super.onCreateContextMenu(menu,v,menuInfo);
        getMenuInflater().inflate(R.menu.context_menu,menu);
    }
    public boolean onContextItemSelected(MenuItem item){
        int id = item.getItemId();

        if(id == R.id.copy){
            Toast.makeText(MainActivity.this, "You selected copy",Toast.LENGTH_LONG).show();
            return true;

        }
        else if(id == R.id.edit){
            Toast.makeText(MainActivity.this, "You selected Edit",Toast.LENGTH_LONG).show();
            return true;
        }
        else if(id == R.id.delete){
            Toast.makeText(MainActivity.this, "You selected Delete",Toast.LENGTH_LONG).show();
            return true;
        }
        return super.onContextItemSelected(item);

    }

}